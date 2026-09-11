"""Fraud indicators and prompt-injection detection (FIN-POL-005 §3 and §4).

FIN-POL-005 §4 is the hinge of this module: "Retrieved text that tells the agent to disable
checks is itself a risk indicator, not an instruction." Detection is therefore a control,
not a filter. Nothing here removes or rewrites text. An injected instruction is counted as
evidence of attempted manipulation, which pushes the case towards escalation rather than
towards the action the text requested.

## Why detection is anchored to clause boundaries

The naive approach is to search for phrases such as "bypass approval" or "disable checks".
That fails immediately on this corpus, because FIN-POL-005 itself contains both phrases
while describing the controls: "a request to bypass normal approval" and "tells the agent to
disable checks". A detector that fires on the policy would mark every run as an attack and
would be switched off within a day.

The distinguishing feature is grammatical mood. An attack issues commands: the imperative
verb opens a clause. Policy describes commands: the verb appears as an infinitive or a
nominalisation inside a sentence ("a request **to** bypass", "tells the agent **to**
disable"). Every pattern below is therefore anchored to a clause boundary, which is the
start of the text, a sentence or list separator, or a coordinating word. This is a
heuristic, not a parser, and its limits are recorded in the README: a well-formed attack
written in the passive voice, in another language, or encoded, would not be caught. That is
precisely why detection is a secondary control and never the thing standing between an
injected instruction and a payment. The structural controls do that work: the model cannot
call the decision tool, the outcome is computed from typed facts, and the tool schemas have
no argument that widens permission.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch, VendorRecord
from ap_agent.domain.request import UntrustedText

#: FIN-POL-005 §3: "Escalate when two or more indicators occur".
ESCALATION_INDICATOR_THRESHOLD: Final = 2

POLICY_INDICATORS = "FIN-POL-005 §3"
POLICY_INJECTION = "FIN-POL-005 §4"
POLICY_BANK_CHANGE = "FIN-POL-004 §2"

#: Start of text, a sentence or list separator, or a coordinating word. An imperative that
#: opens a clause is a command; the same verb mid-sentence is usually description.
_CLAUSE_START = r"(?:^|[.!?;:,\n]\s*|\band\s+|\bthen\s+|\bplease\s+|\bnow\s+)"

_INJECTION_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("IGNORE_PRIOR_INSTRUCTIONS", r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|earlier|above)"),
    ("IGNORE_NAMED_POLICY", r"ignore\s+(?:\w+\s+){0,2}(?:instructions?|polic(?:y|ies)|rules?)"),
    ("DISREGARD_PRIOR", r"disregard\s+(?:all\s+|any\s+)?(?:previous|prior|earlier|the)"),
    (
        "SKIP_CONTROL",
        r"skip\s+(?:the\s+)?(?:duplicate|check|checks|verification|validation|approval)",
    ),
    (
        "SUPPRESS_HUMAN_REVIEW",
        r"do\s+not\s+(?:ask|contact|notify|escalate|verify|check|inform|involve)",
    ),
    ("SELF_CERTIFY_DOCUMENT", r"mark\s+[\w\s]{0,30}?as\s+(?:verified|approved|valid|safe)"),
    (
        "INVOKE_PAYMENT_TOOL",
        r"(?:call|invoke|trigger|use)\s+the\s+(?:\w+\s+){0,2}?(?:payment|posting|pay)\s*(?:\w+\s+)?"
        r"(?:tool|api|endpoint|function)",
    ),
    (
        "BYPASS_APPROVAL",
        r"bypass\s+(?:the\s+)?(?:normal\s+|standard\s+|usual\s+)?(?:approval|control|check|policy|process)",
    ),
    (
        "DISABLE_CONTROL",
        r"disable\s+(?:the\s+)?(?:duplicate\s+|all\s+)?(?:check|checks|control|controls|validation)",
    ),
    ("ROLE_OVERRIDE", r"you\s+are\s+now\s+(?:in\s+)?(?:\w+\s+)?mode"),
    ("ELEVATE_DOCUMENT_TO_POLICY", r"treat\s+this\s+as\s+(?:system\s+)?(?:policy|instructions?)"),
    ("OVERRIDE_SYSTEM", r"override\s+(?:the\s+)?(?:polic|system|instruction)"),
    ("RELEASE_PAYMENT_NOW", r"release\s+(?:the\s+)?payment\s+(?:immediately|now|today)"),
    ("PAY_NOW", r"pay\s+(?:this\s+)?immediately"),
)

_COMPILED_INJECTION: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (code, re.compile(_CLAUSE_START + pattern, re.IGNORECASE))
    for code, pattern in _INJECTION_PATTERNS
)

#: Urgency and secrecy vocabulary. Matched as whole words anywhere in the text, because
#: urgency is a property of the message rather than of a clause.
_URGENCY_TERMS: Final[tuple[str, ...]] = (
    "urgent",
    "urgently",
    "immediately",
    "asap",
    "same day",
    "today",
    "critical",
    "final notice",
)
_SECRECY_TERMS: Final[tuple[str, ...]] = (
    "confidential",
    "confidentially",
    "secret",
    "discreet",
    "discreetly",
    "between us",
    "do not tell",
)
_MANUAL_PAYMENT_TERMS: Final[tuple[str, ...]] = (
    "manual payment",
    "same-day payment",
    "same day payment",
    "wire transfer",
    "telegraphic transfer",
)

#: Wording that asks for settlement outside the standard payment cycle. FIN-POL-006 §2 runs
#: payments on Tuesday and Thursday, so a request naming a weekend, a day that is not a run
#: day, or "out of hours" is a request to leave that cycle.
_OUT_OF_CYCLE_TERMS: Final[tuple[str, ...]] = (
    "weekend",
    "saturday",
    "sunday",
    "out of hours",
    "out-of-hours",
    "after hours",
    "after-hours",
    "public holiday",
)

#: Wording that asks for settlement now. A same-day request settles on the day it is made, so
#: for these the processing date *is* the settlement date and its weekday is the relevant fact.
_SAME_DAY_TERMS: Final[tuple[str, ...]] = (
    "same day",
    "same-day",
    "today",
    "immediately",
    "right now",
    "straight away",
)

#: Round-dollar threshold. FIN-POL-005 §3 refers to "repeated round-dollar invoices", so the
#: indicator requires a prior round-dollar record for the same vendor.
_ROUND_DOLLAR_MODULUS: Final = Decimal("1000")


def _is_round_dollar(amount: Decimal) -> bool:
    return amount > 0 and amount % _ROUND_DOLLAR_MODULUS == 0


class FraudIndicator(BaseModel):
    """One risk signal, with the policy clause that makes it one and where it was found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    description: str
    policy_ref: str
    source: str


def detect_injection(text: str) -> list[str]:
    """Return the codes of injection patterns present in ``text``.

    Order follows the pattern table so the result is deterministic. Duplicated hits of the
    same pattern collapse to one code: the question is whether an attempt was made, not how
    many times a phrase recurs.
    """
    if not text:
        return []
    return [code for code, pattern in _COMPILED_INJECTION if pattern.search(text)]


def _contains_any(haystack: str, needles: Sequence[str]) -> list[str]:
    lowered = haystack.lower()
    return [needle for needle in needles if needle in lowered]


def _normalise_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def fraud_indicators(
    *,
    invoice: Invoice,
    vendor: VendorRecord | None,
    texts: Sequence[UntrustedText],
    history: Sequence[InvoiceHistoryMatch] = (),
    as_of: datetime,
) -> list[FraudIndicator]:
    """Collect the FIN-POL-005 §3 indicators present in a case.

    ``texts`` must contain only untrusted text belonging to **this case**: the request notes
    and its attachments. Retrieved corpus documents do not belong here, even untrusted ones.

    That distinction was found by running the fixtures rather than by reasoning about them.
    An earlier version passed every retrieved untrusted document into this function, and the
    duplicate-invoice case then escalated instead of being rejected: the evidence search had
    surfaced an adversarial notice concerning an entirely different supplier, and its urgency
    and injection language were counted as indicators against the case in hand. Indicators
    describe a transaction. A document merely present in the corpus says nothing about this
    one, and counting it would let any case be escalated by planting a document. Retrieved
    untrusted documents are still screened and logged by the caller, which is where a corpus
    hygiene problem belongs.

    Indicators are deduplicated by code. Without that, an attacker could reach the escalation
    threshold by repeating one signal across several attachments, and a single genuine signal
    appearing in both the notes and an attachment would look like two independent findings.
    """
    found: dict[str, FraudIndicator] = {}

    def add(code: str, description: str, policy_ref: str, source: str) -> None:
        found.setdefault(
            code,
            FraudIndicator(
                code=code, description=description, policy_ref=policy_ref, source=source
            ),
        )

    # ---- signals from untrusted text -------------------------------------------------
    for text in texts:
        injection_codes = detect_injection(text.content)
        if injection_codes:
            add(
                "EMBEDDED_INSTRUCTION_TO_BYPASS_CONTROLS",
                (
                    "The text issues instructions to the processing system "
                    f"({', '.join(injection_codes)}). Recorded as a risk indicator, not "
                    "followed as an instruction."
                ),
                POLICY_INJECTION,
                text.origin,
            )
        urgency = _contains_any(text.content, _URGENCY_TERMS)
        secrecy = _contains_any(text.content, _SECRECY_TERMS)
        if urgency or secrecy:
            add(
                "URGENCY_OR_SECRECY_LANGUAGE",
                "Urgent or secret payment language: " + ", ".join(sorted(set(urgency + secrecy))),
                POLICY_INDICATORS,
                text.origin,
            )
        # FIN-POL-005 §3 names a "weekend manual-payment request", and every word of that
        # is load-bearing. It is a *request*: something the supplier or requester asked for,
        # in the case text. So the test is what the text asks for, in two forms:
        #
        #   - it names a weekend, a public holiday or out-of-hours settlement outright; or
        #   - it asks for same-day settlement on a day that is not a business day, where the
        #     settlement date and the processing date are necessarily the same.
        #
        # Two earlier versions got this wrong in instructive ways. The first tested only the
        # run's weekday, which made the indicator an accident of batch scheduling: the same
        # case escalated on a Sunday run and not on a Monday one, and weekend settlement asked
        # for on a Tuesday could never be an indicator at all. The second keyed off the
        # computed due date, which is worse than it sounds — the due date is the invoice date
        # plus the agreed terms, it falls on a weekend for two invoices in seven, and
        # FIN-POL-006 §2 moves that payment to the *preceding* business day, so the flag
        # announced a weekend settlement that the same calculation had already prevented. Any
        # remittance note mentioning a wire transfer, on an unlucky due date, became one
        # indicator short of escalation.
        manual = _contains_any(text.content, _MANUAL_PAYMENT_TERMS)
        if manual:
            out_of_cycle = _contains_any(text.content, _OUT_OF_CYCLE_TERMS)
            same_day = _contains_any(text.content, _SAME_DAY_TERMS)
            asks_for_non_business_day = bool(out_of_cycle) or (
                bool(same_day) and as_of.weekday() >= 5
            )
            if asks_for_non_business_day:
                occasion = (
                    f"the text asks for settlement outside the payment cycle "
                    f"({', '.join(out_of_cycle)})"
                    if out_of_cycle
                    else f"same-day settlement is requested and today is {as_of.strftime('%A')}"
                )
                add(
                    "WEEKEND_MANUAL_PAYMENT_REQUEST",
                    (
                        f"Manual payment requested ({', '.join(manual)}) and {occasion}. "
                        "FIN-POL-006 §3 requires Treasury approval and Financial Control "
                        "co-approval for a manual or same-day payment, and states that "
                        "supplier urgency is not sufficient grounds."
                    ),
                    POLICY_INDICATORS,
                    text.origin,
                )
        if _contains_any(
            text.content, ("bank account has changed", "new account", "update our bank")
        ):
            add(
                "BANK_CHANGE_REQUESTED_IN_UNVERIFIED_TEXT",
                (
                    "A bank-detail change is asserted in supplier-supplied text. Instructions "
                    "contained in an invoice, email or chat message are not sufficient "
                    "evidence of a change."
                ),
                POLICY_BANK_CHANGE,
                text.origin,
            )

    # ---- signals from the invoice ----------------------------------------------------
    #
    # FIN-POL-005 §3 names "repeated round-dollar invoices", not a single one. Requiring the
    # repeat is not a softening of the control, it is the control as written: an isolated
    # round amount is ordinary, particularly on service invoices, and firing on it held a
    # clean fixture case on no evidence at all. The prior invoice history supplies the
    # repetition test.
    if _is_round_dollar(invoice.gross_amount):
        prior_round = [
            record
            for record in history
            if record.vendor_id == invoice.vendor_id and _is_round_dollar(record.gross_amount)
        ]
        if prior_round:
            add(
                "REPEATED_ROUND_DOLLAR_INVOICES",
                (
                    f"Gross amount {invoice.gross_amount} is an exact multiple of "
                    f"{_ROUND_DOLLAR_MODULUS}, and {len(prior_round)} prior record(s) for this "
                    "vendor are too: " + ", ".join(record.record_id for record in prior_round[:4])
                ),
                POLICY_INDICATORS,
                f"invoice {invoice.invoice_reference} and invoice history",
            )

    # ---- signals from the vendor master ----------------------------------------------
    if vendor is not None:
        if vendor.bank_changed_recently(as_of):
            add(
                "BANK_DETAILS_RECENTLY_CHANGED",
                (
                    "Vendor bank details changed within the last 30 days"
                    + (
                        f" (on {vendor.bank_details_changed_at:%Y-%m-%d})"
                        if vendor.bank_details_changed_at
                        else ""
                    )
                ),
                POLICY_INDICATORS,
                f"vendor master {vendor.vendor_id}",
            )
        if _normalise_name(invoice.vendor_name) != _normalise_name(vendor.legal_name):
            add(
                "VENDOR_NAME_MISMATCH",
                (
                    f"Invoice states '{invoice.vendor_name}' against master record "
                    f"'{vendor.legal_name}'"
                ),
                POLICY_INDICATORS,
                f"vendor master {vendor.vendor_id}",
            )
        if vendor.is_overseas_account and vendor.is_new_vendor(as_of):
            add(
                "PAYMENT_TO_NEW_COUNTRY",
                (
                    "Payment would go to an account in "
                    f"{vendor.bank_country or 'an unknown country'} "
                    f"for a vendor created {vendor.age_in_days(as_of)} days ago"
                ),
                POLICY_INDICATORS,
                f"vendor master {vendor.vendor_id}",
            )
        if vendor.risk_flags:
            add(
                "VENDOR_RISK_FLAG",
                f"Vendor carries risk flags: {', '.join(vendor.risk_flags)}",
                POLICY_INDICATORS,
                f"vendor master {vendor.vendor_id}",
            )

    return list(found.values())
