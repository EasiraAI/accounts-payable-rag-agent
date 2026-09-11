"""The processing request, and the wrapper that marks parts of it untrusted.

``notes`` and ``attachments`` arrive from outside the control environment: a supplier
email, an uploaded PDF, a comment typed by whoever forwarded the case. They sit at the same
trust level as retrieved documents. Rather than relying on every downstream reader to
remember that, they are typed as ``UntrustedText``, which has no accessor that returns a
plain string without an explicit call to ``.content``. Grepping for ``.content`` therefore
enumerates every place untrusted text is read, which is how the trust boundary is audited.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ap_agent.domain.enums import LineType, TrustLevel
from ap_agent.domain.evidence import Invoice, InvoiceLine
from ap_agent.domain.money import MoneyAmount

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=512)]


class UntrustedText(BaseModel):
    """Free text from outside the trust boundary.

    ``origin`` is recorded so that a fraud indicator or citation can say where the text came
    from. ``sha256`` supports the attachment-hash arm of duplicate detection
    (FIN-POL-005 §1) without storing the document twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: Annotated[str, Field(max_length=50_000)]
    origin: NonEmptyStr
    trust: TrustLevel = TrustLevel.UNTRUSTED_CASE_INPUT

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def length(self) -> int:
        return len(self.content)


class Attachment(BaseModel):
    """An attached document, supplied inline as text.

    Binary parsing is out of scope: the agent's behaviour towards attachment *content* is
    what the brief exercises, and an OCR or PDF layer would add failure modes without
    changing the control question. The README records this as a limitation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: NonEmptyStr
    content_type: str = "text/plain"
    text: Annotated[str, Field(max_length=50_000)]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def as_untrusted(self) -> UntrustedText:
        return UntrustedText(content=self.text, origin=f"attachment:{self.filename}")


class RequestLine(BaseModel):
    """An invoice line as supplied on the request.

    Optional. When lines are absent the engine matches on document totals only and records
    that narrowing as an assumption and an unknown, because FIN-POL-002 §1 requires matching
    per line as well as in total. Silently matching only the total would overstate the
    assurance the run provides.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_number: int = Field(ge=1)
    description: NonEmptyStr
    line_type: LineType = LineType.GOODS
    quantity: Annotated[MoneyAmount, Field(ge=0)]
    unit_price: MoneyAmount
    line_total: MoneyAmount
    po_line_number: int | None = None


class ProcessingRequest(BaseModel):
    """Entry point for a run.

    The five mandatory fields are the brief's minimum. The optional fields carry invoice
    detail when the upstream capture system has it; every one of them is treated as a claim
    to be verified against the purchase order and receipt, never as an established fact.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: NonEmptyStr
    invoice_reference: NonEmptyStr
    vendor: NonEmptyStr
    amount: MoneyAmount = Field(gt=0)
    currency: Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")]

    notes: UntrustedText | None = None
    attachments: list[Attachment] = Field(default_factory=list, max_length=20)

    # Optional structured detail.
    vendor_id: str | None = None
    invoice_date: date | None = None
    net_amount: MoneyAmount | None = None
    tax_amount: MoneyAmount | None = None
    po_reference: str | None = None
    payment_terms_days: int | None = None
    lines: list[RequestLine] = Field(default_factory=list)
    requested_by: str | None = None
    #: The cost centre the spend belongs to. Needed to verify that a delegation's scope
    #: covers this case: FIN-POL-003 §4 makes scope a mandatory part of the register entry,
    #: and a scope that is stored but never compared confers authority it should not. A
    #: security review found a facilities-scoped delegation accepted for an
    #: industrial-supplies invoice. An absent cost centre means a scoped delegation cannot
    #: be verified, and is therefore not applied.
    cost_centre: str | None = None

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        object.__setattr__(self, "currency", self.currency.upper())
        return self

    @model_validator(mode="after")
    def _check_component_amounts(self) -> Self:
        """If both components are supplied they must sum to the gross amount.

        This is a validation, not a reconciliation: a request that contradicts itself is
        rejected as invalid input rather than carried forward as a tax variance, which keeps
        ``TAX_QUERY`` meaningful for genuine tax disagreements with a supplier.
        """
        if self.net_amount is not None and self.tax_amount is not None:
            total = self.net_amount + self.tax_amount
            if total != self.amount:
                raise ValueError(
                    f"net_amount + tax_amount = {total} does not equal amount {self.amount}"
                )
        return self

    # ---- derived views ---------------------------------------------------------------

    def untrusted_texts(self) -> list[UntrustedText]:
        """Every untrusted string on the request, in a stable order.

        Used by the risk assessor and by prompt construction. Both call this rather than
        reaching into the fields, so adding a new free-text field cannot accidentally bypass
        injection screening.
        """
        texts: list[UntrustedText] = []
        if self.notes is not None:
            texts.append(self.notes)
        texts.extend(attachment.as_untrusted() for attachment in self.attachments)
        return texts

    def to_invoice(self) -> Invoice:
        """Build the invoice under assessment from the request.

        ``vendor_id`` falls back to the ``vendor`` field when the caller did not supply a
        master-data identifier; the vendor lookup then resolves it or fails, which surfaces
        an unrecognised vendor as missing evidence rather than as a match against the wrong
        record.
        """
        # Three cases, and the middle one used to be wrong. With both components the
        # request is taken as submitted. With neither, the gross stands in for the net and
        # the tax assessment records that it could not be separated. With only the tax, the
        # net is the remainder: an earlier version fell through to ``net = amount`` here, so
        # an invoice submitted as a gross of 1,100 with 100 of tax was read as a net of 1,100
        # *and* 100 of tax. That produced a false tax query, and, when lines were supplied,
        # rejected a perfectly valid invoice for not adding up to a figure it never claimed.
        if self.net_amount is not None:
            net = self.net_amount
            tax = self.tax_amount if self.tax_amount is not None else self.amount - net
        elif self.tax_amount is not None:
            tax = self.tax_amount
            net = self.amount - tax
        else:
            net = self.amount
            tax = Decimal("0.00")
        return Invoice(
            invoice_reference=self.invoice_reference,
            vendor_id=self.vendor_id or self.vendor,
            vendor_name=self.vendor,
            # A missing invoice date is recorded as "today" in UTC and flagged as an
            # assumption by the intake phase; guessing the supplier's local date would
            # silently shift a month-end cut-off under FIN-POL-011 §1.
            invoice_date=self.invoice_date or datetime.now(tz=UTC).date(),
            currency=self.currency,
            net_amount=net,
            tax_amount=tax,
            gross_amount=self.amount,
            po_reference=self.po_reference,
            payment_terms_days=self.payment_terms_days,
            lines=[
                InvoiceLine(
                    line_number=line.line_number,
                    description=line.description,
                    line_type=line.line_type,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                    po_line_number=line.po_line_number,
                )
                for line in self.lines
            ],
            raw_text="\n\n".join(text.content for text in self.untrusted_texts()),
            attachment_hashes=[attachment.sha256 for attachment in self.attachments],
        )

    @property
    def invoice_date_supplied(self) -> bool:
        return self.invoice_date is not None

    @property
    def tax_separated(self) -> bool:
        """Whether the submission stated its tax and net, rather than a gross alone.

        ``to_invoice`` derives a zero tax when neither component is supplied, which makes a
        gross-only submission indistinguishable on the invoice object from a supplier
        declaring an untaxed supply. The two mean opposite things to the tax assessment under
        FIN-POL-002 §2, so the distinction is kept here, at the boundary where it is still
        visible.
        """
        return self.net_amount is not None or self.tax_amount is not None


class ApprovalDecision(BaseModel):
    """The body of an approve or reject callback.

    FIN-POL-003 §5 requires the approval record to identify the approver, their role, the
    applicable limit and the authority-register version. The first two arrive here; the
    limit and register version are resolved by the engine from the role, so an approver
    cannot assert a limit they do not hold.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: NonEmptyStr
    approver_id: NonEmptyStr
    approver_role: str
    comment: Annotated[str, Field(max_length=2_000)] = ""
    delegation_id: str | None = None
