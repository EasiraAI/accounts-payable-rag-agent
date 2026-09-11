"""The tool contracts.

Six tools. Five read, one writes. Each declares its purpose, permission, timeout and retry
budget, and each has an explicit input and output schema.

## On the sixth tool

The brief names five. ``get_authority_delegation`` is added because the brief's own evidence
set includes approval delegations, and FIN-POL-003 §4 makes a delegation valid only if it is
recorded in the authority register with delegate, delegator, scope and dates. That evidence
has to come from somewhere, and folding it into the vendor tool would have given that tool
authority over two unrelated domains. A separate tool keeps each one's permissions
proportionate to what it reads.

## What the input schemas deliberately do not contain

No tool accepts ``force``, ``skip_checks``, ``verified``, ``override`` or any other argument
that widens what it will do. This is the structural half of prompt-injection resistance:
a document can ask for anything, but there is no argument through which the request could be
expressed. The safety property comes from the absence of a field, not from the model
declining to fill one.

``submit_finance_decision`` takes no ``idempotency_key`` from its caller either. The key is
derived inside the tool from the run and the approval, so a caller cannot defeat the
guarantee by varying it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import ApprovalStatus, Outcome, ToolPermission
from ap_agent.domain.errors import ToolPermissionDenied
from ap_agent.domain.evidence import (
    DelegationRecord,
    InvoiceHistoryMatch,
    PurchaseOrder,
    RetrievedChunk,
    VendorRecord,
)
from ap_agent.domain.money import MoneyAmount
from ap_agent.domain.results import DecisionReceipt
from ap_agent.persistence.repository import (
    Repository,
    compute_idempotency_key,
    compute_invoice_fingerprint,
    compute_request_hash,
)
from ap_agent.rag.retriever import Retriever
from ap_agent.tools.base import ToolSpec
from ap_agent.tools.mock_backends import MockBackends

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=256)]

#: The sandbox this system posts to. Named in the receipt and in the README so there is no
#: ambiguity about whether a real ledger was touched.
SIMULATED_POSTING_SYSTEM: Final = "SIMULATED_ERP"


# ---- retrieve_finance_documents --------------------------------------------------------


class RetrieveDocumentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: NonEmptyStr
    top_k: Annotated[int, Field(ge=1, le=20)] = 6
    #: Which document classes may be returned. ``None`` means the evidence set, which
    #: includes externally supplied documents so an injection attempt can be found and
    #: counted. A policy lookup passes the policy classes explicitly.
    doc_types: list[str] | None = None
    purpose: NonEmptyStr = "evidence"


class RetrieveDocumentsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    query_terms: list[str]
    chunks: list[RetrievedChunk]
    result_count: int


RETRIEVE_DOCUMENTS = ToolSpec(
    name="retrieve_finance_documents",
    purpose=(
        "Search the finance policy corpus and return ranked, citable chunks with document "
        "identifier, version, status, section and relevance score. Read-only over a local "
        "index."
    ),
    permission=ToolPermission.READ,
    timeout_seconds=5.0,
    max_retries=1,
)


def retrieve_finance_documents(
    retriever: Retriever,
) -> object:
    """Bind the retrieval tool to an index."""

    def handler(arguments: RetrieveDocumentsInput) -> RetrieveDocumentsOutput:
        chunks = retriever.search(
            arguments.query, top_k=arguments.top_k, doc_types=arguments.doc_types
        )
        return RetrieveDocumentsOutput(
            query=arguments.query,
            query_terms=retriever.index.query_terms(arguments.query),
            chunks=chunks,
            result_count=len(chunks),
        )

    return handler


# ---- get_vendor_record -----------------------------------------------------------------


class GetVendorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: NonEmptyStr


class GetVendorOutput(BaseModel):
    """Vendor master data as the agent is permitted to see it.

    ``found`` is separate from ``vendor`` so that "no such vendor" is a successful call with
    an absent record rather than an error. Absence is evidence, and FIN-POL-001 §5 requires
    it to produce a hold; an exception would instead produce a crash.
    """

    model_config = ConfigDict(extra="forbid")

    found: bool
    vendor: VendorRecord | None = None
    #: Source timestamp, so a stale-data question can be answered from the record itself.
    retrieved_at: datetime


GET_VENDOR_RECORD = ToolSpec(
    name="get_vendor_record",
    purpose=(
        "Retrieve vendor master data: status, masked payment details, risk flags and "
        "last-updated timestamp. Read-only. Cannot modify vendor data, and holds no field "
        "for a full bank account (FIN-POL-004 §3)."
    ),
    permission=ToolPermission.READ,
    timeout_seconds=2.0,
    max_retries=2,
)


def get_vendor_record(backends: MockBackends) -> object:
    def handler(arguments: GetVendorInput) -> GetVendorOutput:
        vendor = backends.get_vendor(arguments.vendor_id)
        return GetVendorOutput(
            found=vendor is not None,
            vendor=vendor,
            retrieved_at=datetime.now(tz=UTC),
        )

    return handler


# ---- get_purchase_order ----------------------------------------------------------------


class GetPurchaseOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_reference: NonEmptyStr


class GetPurchaseOrderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    purchase_order: PurchaseOrder | None = None
    retrieved_at: datetime


GET_PURCHASE_ORDER = ToolSpec(
    name="get_purchase_order",
    purpose=(
        "Retrieve order and receipt data: line items, totals, currency, approval status and "
        "recorded goods receipts. Read-only."
    ),
    permission=ToolPermission.READ,
    timeout_seconds=2.0,
    max_retries=2,
)


def get_purchase_order(backends: MockBackends) -> object:
    def handler(arguments: GetPurchaseOrderInput) -> GetPurchaseOrderOutput:
        order = backends.get_purchase_order(arguments.po_reference)
        return GetPurchaseOrderOutput(
            found=order is not None,
            purchase_order=order,
            retrieved_at=datetime.now(tz=UTC),
        )

    return handler


# ---- check_invoice_history -------------------------------------------------------------


class CheckInvoiceHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: NonEmptyStr
    invoice_reference: NonEmptyStr
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    gross_amount: MoneyAmount


class CheckInvoiceHistoryOutput(BaseModel):
    """Candidate prior records, plus the fingerprint of the invoice they were searched for.

    The tool searches; the rule engine decides what is a duplicate. That split keeps the
    FIN-POL-005 matching rules in one testable place instead of half in a backend.

    ``exact_fingerprint_matches`` exists because the declared input carried the invoice
    reference, currency and amount while the handler ignored all three and returned the
    vendor's whole history. A review noted that the contract was wider than the
    implementation. The fields are now used: the tool reports which candidates match on all
    four FIN-POL-005 §1 exact-match fields, and still returns everything for the engine to
    classify.
    """

    model_config = ConfigDict(extra="forbid")

    candidates: list[InvoiceHistoryMatch]
    candidate_count: int
    #: Record identifiers matching on vendor, normalised invoice number, currency and gross.
    exact_fingerprint_matches: list[str] = Field(default_factory=list)
    statuses_searched: list[str]
    retrieved_at: datetime


CHECK_INVOICE_HISTORY = ToolSpec(
    name="check_invoice_history",
    purpose=(
        "Return candidate prior invoice records for a vendor across paid, posted, held and "
        "rejected history, with stable record identifiers and statuses. Read-only. Returns "
        "candidates for the rule engine to classify, not duplicate verdicts."
    ),
    permission=ToolPermission.READ,
    timeout_seconds=2.0,
    max_retries=2,
)


def check_invoice_history(backends: MockBackends) -> object:
    def handler(arguments: CheckInvoiceHistoryInput) -> CheckInvoiceHistoryOutput:
        candidates = backends.find_history(vendor_id=arguments.vendor_id)
        wanted = compute_invoice_fingerprint(
            vendor_id=arguments.vendor_id,
            invoice_reference=arguments.invoice_reference,
            currency=arguments.currency,
            gross_amount=arguments.gross_amount,
        )
        exact = [
            record.record_id
            for record in candidates
            if compute_invoice_fingerprint(
                vendor_id=record.vendor_id,
                invoice_reference=record.invoice_reference,
                currency=record.currency,
                gross_amount=record.gross_amount,
            )
            == wanted
        ]
        return CheckInvoiceHistoryOutput(
            candidates=candidates,
            candidate_count=len(candidates),
            exact_fingerprint_matches=exact,
            statuses_searched=["PAID", "POSTED", "HELD", "REJECTED"],
            retrieved_at=datetime.now(tz=UTC),
        )

    return handler


# ---- get_authority_delegation ----------------------------------------------------------


class GetDelegationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation_id: NonEmptyStr


class GetDelegationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    delegation: DelegationRecord | None = None
    register_version: str = ""
    retrieved_at: datetime


GET_AUTHORITY_DELEGATION = ToolSpec(
    name="get_authority_delegation",
    purpose=(
        "Retrieve a delegation from the authority register: delegate, delegator, scope, "
        "start and end dates, and revocation state (FIN-POL-003 §4). Read-only. Added "
        "beyond the five named tools because approval-delegation evidence needs its own "
        "bounded source rather than being folded into the vendor tool."
    ),
    permission=ToolPermission.READ,
    timeout_seconds=2.0,
    max_retries=2,
)


def get_authority_delegation(backends: MockBackends) -> object:
    def handler(arguments: GetDelegationInput) -> GetDelegationOutput:
        delegation = backends.get_delegation(arguments.delegation_id)
        return GetDelegationOutput(
            found=delegation is not None,
            delegation=delegation,
            register_version=delegation.register_version if delegation else "",
            retrieved_at=datetime.now(tz=UTC),
        )

    return handler


# ---- submit_finance_decision -----------------------------------------------------------


class SubmitFinanceDecisionInput(BaseModel):
    """Arguments for recording a consequential outcome.

    There is no idempotency key here: it is derived inside the tool from the run and the
    approval. There is no override flag, no force, and no way to assert that approval was
    obtained. The tool checks the approval record itself.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: NonEmptyStr
    case_id: NonEmptyStr
    approval_id: NonEmptyStr
    outcome: Outcome
    amount: MoneyAmount = Field(gt=Decimal("0"))
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    vendor_id: NonEmptyStr
    #: The invoice reference, needed for the cross-run invoice fingerprint. Not an
    #: authorisation field: it identifies what is being posted, not permission to post it.
    invoice_reference: NonEmptyStr


SUBMIT_FINANCE_DECISION = ToolSpec(
    name="submit_finance_decision",
    purpose=(
        "Record a consequential outcome (posting, rejection) against a simulated ledger. "
        "Deny-by-default: refuses unless the repository holds an approval for this run whose "
        "signature requirement is met, whose outcome matches, and whose presented amount, "
        "currency and vendor match the arguments. Idempotent on a key derived from the run, "
        "the approval and the monetary facts. Refuses a second posting of the same invoice "
        "across runs. Targets a simulated system and is incapable of moving money: there is "
        "no payment rail, no bank credential and no network call in this code path."
    ),
    permission=ToolPermission.WRITE,
    timeout_seconds=5.0,
    # Not retried by the runner. A write is driven by the orchestrator, which relies on the
    # idempotency key rather than on a retry loop, so that a partially-applied attempt
    # cannot be compounded by a second one.
    max_retries=0,
)


def submit_finance_decision(repository: Repository) -> object:
    """Bind the decision tool to the store that holds its authorisation and its ledger.

    The tool re-checks the approval itself rather than trusting its caller. The orchestrator
    already checks before calling, so this is the second of two independent gates. Duplicated
    deliberately: a single enforcement point for the property that keeps money from moving is
    a single point of failure.
    """

    def handler(arguments: SubmitFinanceDecisionInput) -> DecisionReceipt:
        approval = repository.load_approval(arguments.approval_id)
        if approval is None:
            raise ToolPermissionDenied(
                SUBMIT_FINANCE_DECISION.name,
                f"no approval record {arguments.approval_id}; a consequential outcome "
                "cannot be recorded without one",
            )
        if approval.run_id != arguments.run_id:
            raise ToolPermissionDenied(
                SUBMIT_FINANCE_DECISION.name,
                f"approval {arguments.approval_id} belongs to run {approval.run_id}, "
                f"not {arguments.run_id}",
            )
        if approval.status is not ApprovalStatus.APPROVED:
            raise ToolPermissionDenied(
                SUBMIT_FINANCE_DECISION.name,
                f"approval {arguments.approval_id} is {approval.status.value}; only an "
                "APPROVED approval authorises a decision (FIN-POL-001 §3)",
            )
        if approval.requested_outcome is not arguments.outcome:
            # The approver decided on a specific outcome. Recording a different one would
            # use their authority for something they did not see.
            raise ToolPermissionDenied(
                SUBMIT_FINANCE_DECISION.name,
                f"approval {arguments.approval_id} authorises "
                f"{approval.requested_outcome.value}, not {arguments.outcome.value}",
            )
        if not approval.signature_requirement_met:
            # FIN-POL-003 §3 requires two approvals for a higher-risk transaction, one from
            # Financial Control. An earlier version computed that requirement, displayed it
            # to the approver, and let a single signature post: two independent reviews
            # found the same gap. The tool now refuses until the requirement is met, and the
            # gate refuses independently.
            raise ToolPermissionDenied(
                SUBMIT_FINANCE_DECISION.name,
                f"approval {arguments.approval_id} does not yet meet its signature "
                f"requirement: {approval.outstanding_requirement_detail()}",
            )

        # The decision must be for the transaction the approver was shown. A security review
        # parked a run at the gate, mutated the request amount, and watched the inflated
        # figure post: both gates compared only the run and the outcome, while the approval
        # record held the presented amount all along and nothing read it.
        presented = (
            approval.presented_amount,
            approval.presented_currency.upper(),
            approval.presented_vendor_id,
        )
        supplied = (arguments.amount, arguments.currency.upper(), arguments.vendor_id)
        if approval.presented_vendor_id and presented != supplied:
            raise ToolPermissionDenied(
                SUBMIT_FINANCE_DECISION.name,
                f"approval {arguments.approval_id} authorises "
                f"{approval.presented_amount} {approval.presented_currency} to vendor "
                f"{approval.presented_vendor_id}, not {arguments.amount} "
                f"{arguments.currency} to {arguments.vendor_id}; an approver's authority "
                "applies to the figures they were shown",
            )

        idempotency_key = compute_idempotency_key(
            run_id=arguments.run_id,
            approval_id=arguments.approval_id,
            outcome=arguments.outcome,
            amount=arguments.amount,
            currency=arguments.currency,
            vendor_id=arguments.vendor_id,
        )
        invoice_fingerprint = compute_invoice_fingerprint(
            vendor_id=arguments.vendor_id,
            invoice_reference=arguments.invoice_reference,
            currency=arguments.currency,
            gross_amount=arguments.amount,
        )
        request_hash = compute_request_hash(arguments.model_dump(mode="json"))

        receipt = DecisionReceipt(
            decision_ref=f"DEC-{idempotency_key[:12].upper()}",
            run_id=arguments.run_id,
            case_id=arguments.case_id,
            outcome=arguments.outcome,
            amount=arguments.amount,
            currency=arguments.currency,
            idempotency_key=idempotency_key,
            recorded_at=datetime.now(tz=UTC),
            simulated=True,
            posting_system=SIMULATED_POSTING_SYSTEM,
        )
        stored, _replayed = repository.record_decision(
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            approval_id=arguments.approval_id,
            invoice_fingerprint=invoice_fingerprint,
            receipt=receipt,
        )
        return stored

    return handler


#: Every tool contract, for the manifest and the documentation. Generated from the specs so
#: the manifest cannot drift from the code.
ALL_TOOL_SPECS: Final[list[ToolSpec]] = [
    RETRIEVE_DOCUMENTS,
    GET_VENDOR_RECORD,
    GET_PURCHASE_ORDER,
    CHECK_INVOICE_HISTORY,
    GET_AUTHORITY_DELEGATION,
    SUBMIT_FINANCE_DECISION,
]
