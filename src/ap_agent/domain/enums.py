"""Closed vocabularies.

Every value here is taken verbatim from the policy corpus. Using string enums rather than
free text means an invented outcome such as "APPROVED_WITH_CONDITIONS" fails validation at
the boundary instead of reaching an approver, and it lets the reconciliation engine express
precedence as a total order over a known set.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """Processing outcomes permitted by FIN-POL-001 §3.

    Ordered by severity for precedence resolution; see ``domain.rules.outcome``.
    """

    APPROVE_FOR_POSTING = "APPROVE_FOR_POSTING"
    HOLD_FOR_INFORMATION = "HOLD_FOR_INFORMATION"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"
    REJECT_INVALID = "REJECT_INVALID"
    ESCALATE_CONTROL_REVIEW = "ESCALATE_CONTROL_REVIEW"

    @property
    def is_consequential(self) -> bool:
        """True when recording this outcome requires prior human approval.

        Posting and rejection both write to the ledger of record, so both are gated. A
        hold or an escalation moves the case inside accounts payable without an external
        effect, so neither requires an approver to proceed.
        """
        return self in {
            Outcome.APPROVE_FOR_POSTING,
            Outcome.REJECT_DUPLICATE,
            Outcome.REJECT_INVALID,
        }

    @property
    def proposes_payment(self) -> bool:
        """True only for the outcome that leads to money leaving the organisation."""
        return self is Outcome.APPROVE_FOR_POSTING


class ExceptionCategory(StrEnum):
    """Primary exception categories from FIN-POL-007 §1. Exactly one per exception record."""

    MISSING_PO = "MISSING_PO"
    MISSING_RECEIPT = "MISSING_RECEIPT"
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
    DUPLICATE_RISK = "DUPLICATE_RISK"
    VENDOR_BLOCK = "VENDOR_BLOCK"
    BANK_CHANGE = "BANK_CHANGE"
    AUTHORITY_GAP = "AUTHORITY_GAP"
    TAX_QUERY = "TAX_QUERY"
    OTHER_CONTROL_RISK = "OTHER_CONTROL_RISK"


class EscalationOwner(StrEnum):
    """Escalation targets from FIN-POL-007 §3."""

    REQUESTER = "REQUESTER"
    RECEIPTER = "RECEIPTER"
    VENDOR_GOVERNANCE = "VENDOR_GOVERNANCE"
    FINANCIAL_CONTROL = "FINANCIAL_CONTROL"
    FINANCIAL_CRIME_AND_CONTROLS = "FINANCIAL_CRIME_AND_CONTROLS"
    ACCOUNTS_PAYABLE_MANAGER = "ACCOUNTS_PAYABLE_MANAGER"
    TREASURY = "TREASURY"


class VendorStatus(StrEnum):
    """Vendor master statuses. FIN-POL-004 §4 holds every status except ACTIVE."""

    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    DORMANT = "DORMANT"
    SANCTIONS_REVIEW = "SANCTIONS_REVIEW"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"

    @property
    def blocks_processing(self) -> bool:
        return self is not VendorStatus.ACTIVE


class ApproverRole(StrEnum):
    """Roles in the FIN-POL-003 v4.0 delegated authority matrix.

    FINANCIAL_CONTROL carries no monetary limit of its own; it satisfies the
    "one approver must be from Financial Control" requirement in §3.
    """

    COST_CENTRE_MANAGER = "COST_CENTRE_MANAGER"
    DEPARTMENT_DIRECTOR = "DEPARTMENT_DIRECTOR"
    EXECUTIVE_DIRECTOR = "EXECUTIVE_DIRECTOR"
    CFO = "CFO"
    CEO = "CEO"
    FINANCIAL_CONTROL = "FINANCIAL_CONTROL"


class LineType(StrEnum):
    """Line classification. Drives which tolerance band applies (FIN-POL-002 §2)."""

    GOODS = "GOODS"
    SERVICE = "SERVICE"
    FREIGHT = "FREIGHT"


class InvoiceHistoryStatus(StrEnum):
    """Statuses of records searched during duplicate detection (FIN-POL-005 §1)."""

    PAID = "PAID"
    POSTED = "POSTED"
    HELD = "HELD"
    REJECTED = "REJECTED"


class DocumentStatus(StrEnum):
    """Corpus document lifecycle, read from each document's front matter.

    ``UNTRUSTED`` is not a lifecycle state so much as a provenance one: the document came
    from outside the control environment. It is kept in the same enum because retrieval
    treats all three the same way, by ranking and labelling rather than by exclusion.
    """

    CURRENT = "current"
    SUPERSEDED = "superseded"
    UNTRUSTED = "untrusted"

    @property
    def is_authoritative(self) -> bool:
        return self is DocumentStatus.CURRENT


class TrustLevel(StrEnum):
    """Trust levels from the trust-boundary model. Nothing crosses upward."""

    TRUSTED_POLICY = "TRUSTED_POLICY"
    UNTRUSTED_EVIDENCE = "UNTRUSTED_EVIDENCE"
    UNTRUSTED_CASE_INPUT = "UNTRUSTED_CASE_INPUT"


class RunPhase(StrEnum):
    """Phases of the state machine, in execution order.

    ``HELD``, ``COMPLETED`` and ``FAILED`` are terminal. The driver refuses to advance
    from a terminal phase, which is what makes a replayed callback a no-op rather than a
    second pass through the machine.
    """

    INTAKE = "INTAKE"
    RETRIEVE_POLICY = "RETRIEVE_POLICY"
    GATHER_EVIDENCE = "GATHER_EVIDENCE"
    RECONCILE = "RECONCILE"
    ASSESS_RISK = "ASSESS_RISK"
    RECOMMEND = "RECOMMEND"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTE_DECISION = "EXECUTE_DECISION"
    COMPLETED = "COMPLETED"
    HELD = "HELD"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {RunPhase.COMPLETED, RunPhase.HELD, RunPhase.FAILED}


class RunStatus(StrEnum):
    """Externally visible run status. Coarser than ``RunPhase`` on purpose.

    Callers of the HTTP API should branch on status; phase is diagnostic detail that may
    change as the machine is refined.
    """

    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    HELD = "HELD"
    FAILED = "FAILED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FailureReason(StrEnum):
    """Why a run reached ``FAILED``. Every value is recoverable by a human except
    ``INTERNAL_ERROR``, which indicates a defect rather than a data problem."""

    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPROVAL_STATE_CONFLICT = "APPROVAL_STATE_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolPermission(StrEnum):
    """Least-authority classification for every tool.

    ``READ`` tools may be called by the orchestrator at will inside the budget. ``WRITE``
    tools are deny-by-default: the orchestrator must present an approved approval record,
    and the tool re-checks it. There is no third level, and no tool is permitted to widen
    its own authority through an argument.
    """

    READ = "READ"
    WRITE = "WRITE"


class EventType(StrEnum):
    """Audit event vocabulary. Append-only; renaming a member breaks stored history."""

    RUN_CREATED = "RUN_CREATED"
    RUN_RESUMED = "RUN_RESUMED"
    PHASE_STARTED = "PHASE_STARTED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    RETRIEVAL = "RETRIEVAL"
    TOOL_CALL = "TOOL_CALL"
    MODEL_CALL = "MODEL_CALL"
    RULE_EVALUATED = "RULE_EVALUATED"
    EXCEPTION_RAISED = "EXCEPTION_RAISED"
    RECOMMENDATION_READY = "RECOMMENDATION_READY"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    APPROVAL_REPLAYED = "APPROVAL_REPLAYED"
    DECISION_SUBMITTED = "DECISION_SUBMITTED"
    DECISION_REPLAYED = "DECISION_REPLAYED"
    INJECTION_ATTEMPT_DETECTED = "INJECTION_ATTEMPT_DETECTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"


class ToolOutcome(StrEnum):
    """Result classification recorded on every ``TOOL_CALL`` event."""

    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"
    DENIED = "DENIED"
