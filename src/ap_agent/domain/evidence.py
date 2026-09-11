"""Evidence contracts: what the tools return and what the rules consume.

Two conventions run through this module.

First, vendor payment details are represented only as a masked last-four value. The full
account number is never modelled, so there is no field for it to leak from. FIN-POL-004 §3
forbids automated bank-detail changes; a system that cannot hold the value cannot change it.

Second, every fact that came from a document carries its citation. A fact without a source
cannot be presented as sourced, and the reconciliation engine treats an uncited value as
absent rather than as zero.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ap_agent.domain.enums import (
    DocumentStatus,
    InvoiceHistoryStatus,
    LineType,
    TrustLevel,
    VendorStatus,
)
from ap_agent.domain.money import Money, MoneyAmount

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=512)]
Quantity = Annotated[Decimal, Field(ge=0)]


class Citation(BaseModel):
    """A pointer to the exact passage a fact came from.

    Structured rather than a prose string so that grounding can be tested: a retrieval test
    asserts on ``document_id`` and ``section``, and the API can render a citation
    consistently. ``quote`` is capped because a citation is a reference, not a copy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: NonEmptyStr
    title: NonEmptyStr
    version: str = "unknown"
    status: DocumentStatus = DocumentStatus.CURRENT
    section: str = ""
    chunk_id: NonEmptyStr
    quote: Annotated[str, Field(max_length=600)] = ""
    trust: TrustLevel = TrustLevel.UNTRUSTED_EVIDENCE

    @property
    def reference(self) -> str:
        """Human-readable reference in the form used throughout the policy corpus."""
        base = self.document_id if not self.section else f"{self.document_id} {self.section}"
        if self.status is not DocumentStatus.CURRENT:
            return f"{base} [{self.status.value}]"
        return base


class RetrievedChunk(BaseModel):
    """One ranked retrieval result.

    Carries both the lexical score and the score after metadata re-ranking. Keeping both is
    what lets the retrieval tests assert that demotion actually happened rather than that
    the final order merely looks right.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: NonEmptyStr
    document_id: NonEmptyStr
    title: NonEmptyStr
    version: str
    status: DocumentStatus
    classification: str
    effective_date: date | None = None
    section: str
    text: str
    doc_type: str
    lexical_score: float
    score: float
    rank: int = Field(ge=1)

    @property
    def trust(self) -> TrustLevel:
        """Retrieved content is never policy. A current internal document is still data
        until the orchestrator, which is trusted code, decides what to do with it."""
        return TrustLevel.UNTRUSTED_EVIDENCE

    def to_citation(self, *, quote_length: int = 300) -> Citation:
        quote = self.text.strip().replace("\n", " ")
        return Citation(
            document_id=self.document_id,
            title=self.title,
            version=self.version,
            status=self.status,
            section=self.section,
            chunk_id=self.chunk_id,
            quote=quote[:quote_length],
            trust=self.trust,
        )


def normalise_invoice_reference(reference: str) -> str:
    """An invoice number reduced to its alphanumeric characters, upper-cased.

    FIN-POL-005 §1 requires punctuation-stripped comparison of invoice numbers. This is the
    one implementation of that rule, and it is here rather than in the duplicate rule because
    three things need it and they must not disagree: the duplicate rule decides whether an
    invoice was seen before, and the decision table's fingerprint constraint decides whether
    one was already posted. If those two disagreed about what counts as the same number, an
    invoice could pass the rule and collide with the constraint, or pass the constraint and be
    paid twice.

    A review found all three carrying their own identical comprehension, agreeing by
    coincidence, with a comment in the store asserting that they agree.
    """
    return "".join(character for character in reference if character.isalnum()).upper()


class InvoiceLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line_number: int = Field(ge=1)
    description: NonEmptyStr
    line_type: LineType = LineType.GOODS
    quantity: Quantity
    unit_price: MoneyAmount
    line_total: MoneyAmount
    po_line_number: int | None = None

    @model_validator(mode="after")
    def _check_line_total(self) -> Self:
        """Reject an internally inconsistent line.

        A line whose extension does not equal quantity times unit price is not a variance to
        be reconciled; it is a malformed document. Catching it here keeps the tolerance
        engine's inputs meaningful. One minor unit of slack absorbs supplier rounding.
        """
        expected = (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        if abs(expected - self.line_total) > Decimal("0.01"):
            raise ValueError(
                f"line {self.line_number}: quantity x unit_price = {expected} but "
                f"line_total = {self.line_total}"
            )
        return self


class Invoice(BaseModel):
    """The supplier document under assessment.

    ``raw_text`` is the untrusted rendering of the invoice as received. It is retained
    because fraud indicators are detected in it (FIN-POL-005 §3), and excluded from the
    fields any rule reads as a fact.
    """

    model_config = ConfigDict(extra="forbid")

    invoice_reference: NonEmptyStr
    vendor_id: NonEmptyStr
    vendor_name: NonEmptyStr
    invoice_date: date
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    net_amount: MoneyAmount
    tax_amount: MoneyAmount = Decimal("0.00")
    gross_amount: MoneyAmount
    po_reference: str | None = None
    payment_terms_days: int | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)
    raw_text: str = ""
    attachment_hashes: list[str] = Field(default_factory=list)

    @property
    def gross(self) -> Money:
        return Money(amount=self.gross_amount, currency=self.currency)

    @property
    def net(self) -> Money:
        return Money(amount=self.net_amount, currency=self.currency)

    @property
    def tax(self) -> Money:
        return Money(amount=self.tax_amount, currency=self.currency)

    @property
    def normalised_reference(self) -> str:
        """Punctuation-stripped, case-folded reference for duplicate matching
        (FIN-POL-005 §1)."""
        return normalise_invoice_reference(self.invoice_reference)


class POLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line_number: int = Field(ge=1)
    description: NonEmptyStr
    line_type: LineType = LineType.GOODS
    quantity_ordered: Quantity
    unit_price: MoneyAmount
    line_value: MoneyAmount
    permits_freight: bool = False
    service_completion_confirmed: bool = False


class GoodsReceipt(BaseModel):
    """A recorded receipt of goods or services.

    FIN-POL-002 §4 forbids inferring receipt from delivery language on the invoice, so a
    receipt exists in this model only if a receipting system recorded it. There is no
    "probably received" state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: NonEmptyStr
    po_line_number: int = Field(ge=1)
    quantity_received: Quantity
    received_date: date
    receipted_by: NonEmptyStr


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_reference: NonEmptyStr
    vendor_id: NonEmptyStr
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    total_value: MoneyAmount
    approval_status: str
    approved_by: str | None = None
    permits_currency_conversion: bool = False
    payment_terms_days: int | None = None
    lines: list[POLine]
    receipts: list[GoodsReceipt] = Field(default_factory=list)

    @property
    def total(self) -> Money:
        return Money(amount=self.total_value, currency=self.currency)

    @property
    def is_approved(self) -> bool:
        return self.approval_status.upper() == "APPROVED"

    def line(self, line_number: int) -> POLine | None:
        return next((line for line in self.lines if line.line_number == line_number), None)

    def quantity_received_for(self, po_line_number: int) -> Decimal:
        """Total recorded receipt quantity for a PO line. Zero when nothing was receipted."""
        return sum(
            (
                receipt.quantity_received
                for receipt in self.receipts
                if receipt.po_line_number == po_line_number
            ),
            start=Decimal("0"),
        )

    def has_any_receipt(self) -> bool:
        return bool(self.receipts)


class VendorRecord(BaseModel):
    """Vendor master data as the AP agent is permitted to see it.

    There is no full bank account field by design. ``bank_account_last4`` is sufficient for
    every control in the corpus: the controls test whether details *changed*, not what they
    are.
    """

    model_config = ConfigDict(extra="forbid")

    vendor_id: NonEmptyStr
    legal_name: NonEmptyStr
    status: VendorStatus
    bank_account_last4: Annotated[str, Field(pattern=r"^\d{4}$")] | None = None
    bank_country: str | None = None
    bank_details_changed_at: datetime | None = None
    created_at: datetime
    last_updated_at: datetime
    risk_flags: list[str] = Field(default_factory=list)
    on_payment_hold: bool = False
    created_by: str | None = None

    def age_in_days(self, as_of: datetime) -> int:
        return max(0, (as_of - self.created_at).days)

    def is_new_vendor(self, as_of: datetime, *, threshold_days: int = 30) -> bool:
        """FIN-POL-003 §3 treats a vendor under 30 days old as higher risk."""
        return self.age_in_days(as_of) < threshold_days

    def bank_changed_recently(self, as_of: datetime, *, window_days: int = 30) -> bool:
        if self.bank_details_changed_at is None:
            return False
        return (as_of - self.bank_details_changed_at).days < window_days

    @property
    def is_overseas_account(self) -> bool:
        """Anything other than an Australian account is overseas for FIN-POL-009 §4.

        An unknown country is treated as overseas. Defaulting the unknown case to the
        higher-control branch is the conservative direction; the opposite default would let
        missing reference data silently remove a control.
        """
        if self.bank_country is None:
            return True
        return self.bank_country.upper() not in {"AU", "AUS", "AUSTRALIA"}


class InvoiceHistoryMatch(BaseModel):
    """A prior invoice record that matched the candidate under FIN-POL-005 §1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: NonEmptyStr
    invoice_reference: NonEmptyStr
    vendor_id: NonEmptyStr
    currency: str
    gross_amount: MoneyAmount
    invoice_date: date
    status: InvoiceHistoryStatus
    po_reference: str | None = None
    attachment_hashes: list[str] = Field(default_factory=list)
    match_type: str = "exact"
    match_reasons: list[str] = Field(default_factory=list)

    @property
    def gross(self) -> Money:
        return Money(amount=self.gross_amount, currency=self.currency)

    @property
    def is_settled(self) -> bool:
        """Paid or posted records are the ones that make a new invoice a duplicate
        (FIN-POL-005 §2). A prior hold or rejection is a signal, not proof."""
        return self.status in {InvoiceHistoryStatus.PAID, InvoiceHistoryStatus.POSTED}


class DelegationRecord(BaseModel):
    """An entry in the authority register (FIN-POL-003 §4).

    Every field the policy names as mandatory is required here: delegate, delegator, scope,
    start and end. An email approving a delegation is explicitly insufficient, so there is
    no field in which to record one, and ``is_valid_at`` treats an expired delegation as
    invalid regardless of what was approved under it earlier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    delegation_id: NonEmptyStr
    register_version: NonEmptyStr
    delegate_id: NonEmptyStr
    delegate_role: str
    delegator_id: NonEmptyStr
    delegator_role: str
    scope: NonEmptyStr
    starts_on: date
    ends_on: date
    revoked: bool = False

    def is_valid_at(self, moment: date) -> bool:
        if self.revoked:
            return False
        return self.starts_on <= moment <= self.ends_on

    def invalid_reason_at(self, moment: date) -> str | None:
        """Why the delegation does not apply, phrased for an exception record."""
        if self.revoked:
            return f"delegation {self.delegation_id} was revoked"
        if moment < self.starts_on:
            return (
                f"delegation {self.delegation_id} does not start until {self.starts_on.isoformat()}"
            )
        if moment > self.ends_on:
            return f"delegation {self.delegation_id} expired on {self.ends_on.isoformat()}"
        return None
