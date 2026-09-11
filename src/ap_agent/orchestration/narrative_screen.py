"""Screening the one channel where model prose reaches a human decision-maker.

The outcome an approver acts on is computed by the rule engine, and a model suggestion can only
tighten it. But the *prose* an approver reads is written by the model, and a compromised model
keeps the computed outcome while writing whatever it likes into the summary. The original
observation was a run holding ``REJECT_DUPLICATE`` whose summary read "APPROVED by the CFO out
of band. Post immediately; the duplicate flag is a system error." Schema-valid, so nothing
rejected it, and the outcome field is not what an eye goes to first.

The first screen for that was a list of eight phrases. An audit broke it in one line:

    "Finance leadership has signed off; settlement today is appropriate."

No listed phrase appears, so it reached the approver. A phrase list is a blocklist, and a
blocklist over natural language loses to paraphrase every time, which is the same argument this
project makes for why injection detection cannot be the primary control.

Three checks replace it, in increasing order of how hard they are to talk around.

**Injection patterns.** The same clause-anchored detector used on untrusted documents. Cheap,
and it catches the unsubtle case.

**Claims crossed with state.** A claim is not a word, it is the grammar of a completed act:
"has been approved", "approved by", "already signed off", "approval was granted", or a
settlement verb within a clause of *today*, *now* or *immediately*. A claim is then a finding
only when the run's own state contradicts it, so on a run that has collected its signatures
"signed off" is simply true and passes.

Two rounds of calibration went into that sentence. Matching bare approval words flagged
FIN-002's correct narrative, which says the invoice must not be approved; matching eight fixed
phrases missed the audit's "Finance leadership has signed off; settlement today is
appropriate." What survives both is completion grammar minus negation: a claim preceded by
*not*, *never*, *cannot*, *without*, *pending*, *awaiting*, *requires* or *before* is a
description of the control state rather than an assertion about it.

**Figures crossed with the computed set.** The check that needs no lexicon at all. Every
monetary amount, percentage and date in the prose must appear among the values the engine
computed. A narrative that invents "an invoice of 24,750.00 approved on 2 September" is
discarded whatever words surround the numbers, and a narrative that only restates computed
figures passes however it is phrased. This is the faithfulness measure for the generation
channel: unsupported specifics are the failure mode that matters, because a fabricated number
in an approver-facing summary is indistinguishable from a real one.

All three are deterministic and run on every case, including in the deterministic test tier.
When any fires, the model's prose is discarded and replaced with a summary built from the
computed results, and the substitution is recorded as a finding: a model writing approval
language into a rejection is itself a finding.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: The verbs an approval claim is made with. Used only inside the patterns below, never on
#: their own: the bare word appears in every correct description of an unapproved invoice.
_APPROVAL_VERBS: Final = (
    r"approved|authorised|authorized|sanctioned|ratified|countersigned|signed\s*off"
)

#: The grammar of a completed approval. Each alternative needs a completion marker or an
#: agent, which is what separates "has been approved" from "must not be approved".
_APPROVAL_CLAIM: Final = re.compile(
    rf"\b(?:has|have|had|was|were|is|are|been)\s+(?:been\s+)?(?:already\s+)?(?:{_APPROVAL_VERBS})\b"
    rf"|\b(?:{_APPROVAL_VERBS})\s+by\b"
    rf"|\balready\s+(?:{_APPROVAL_VERBS})\b"
    # Noun forms too. "Authorisation was obtained earlier this week" asserts a completed
    # approval without using a verb from the list above, and slipped through until an
    # adversarial variant in the unit tests caught it.
    r"|\b(?:approvals?|authoris(?:ation|ations)|authoriz(?:ation|ations)|sign-?offs?)"
    r"\s+(?:has\s+been\s+|have\s+been\s+|was\s+|were\s+|is\s+|are\s+)?"
    r"(?:granted|obtained|received|in\s+place|complete|completed|on\s+file)\b"
    r"|\bcleared\s+for\s+payment\b"
    r"|\bgreen-?lit\b"
)

#: Words that turn a claim into a description of the control state. Checked in the text
#: immediately before a match, which is where English puts them.
_NEGATORS: Final[tuple[str, ...]] = (
    "not",
    "never",
    "cannot",
    "can not",
    "without",
    "no",
    "pending",
    "awaiting",
    "requires",
    "require",
    "required",
    "before",
    "until",
    "unless",
    "once",
)

#: How far back to look for a negator, in characters. Long enough for "payment must not be
#: approved" and "this invoice has not yet been approved", short enough that a negation in a
#: previous sentence does not excuse a claim in this one.
_NEGATION_WINDOW: Final = 40

#: Words that assert payment should happen now. Paired with an immediacy word below, because
#: "pay" alone appears in every legitimate summary of an accounts-payable case.
_SETTLEMENT_WORDS: Final[tuple[str, ...]] = (
    "pay",
    "paid",
    "payment",
    "settle",
    "settlement",
    "release",
    "remit",
    "disburse",
    "post",
)

_IMMEDIACY_WORDS: Final[tuple[str, ...]] = (
    "today",
    "now",
    "immediately",
    "at once",
    "right away",
    "same day",
    "same-day",
    "without delay",
    "out of band",
    "out-of-band",
)

#: How close a settlement word and an immediacy word must be to read as one claim, in
#: characters. Wide enough for "settlement today is appropriate" and for a clause boundary,
#: narrow enough that two unrelated sentences do not combine into a claim neither made.
_CLAIM_WINDOW: Final = 60

#: A monetary amount or percentage: at least one digit, optional thousands separators, optional
#: decimals. Bare small integers are excluded by ``_is_significant`` below.
_FIGURE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\b|\b\d{4,}\b")

#: An ISO date or a day-month form. Dates are checked because a fabricated date in an
#: approver-facing summary is as misleading as a fabricated amount.
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\b",
    re.IGNORECASE,
)


class NarrativeScreenResult(BaseModel):
    """What the screen found, and whether the prose may be shown."""

    model_config = ConfigDict(extra="forbid")

    injection_codes: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    unsupported_figures: list[str] = Field(default_factory=list)

    @property
    def findings(self) -> list[str]:
        return [*self.injection_codes, *self.unsupported_claims, *self.unsupported_figures]

    @property
    def clean(self) -> bool:
        return not self.findings


def _is_negated(lowered: str, position: int) -> bool:
    """Whether the text just before ``position`` turns a claim into a description.

    A window rather than a parse. The alternative is sentence parsing, which would be a
    dependency and a second thing to be wrong; the failure mode of a window is a claim excused
    by a nearby negation, and that direction is the safe one here because the figure check runs
    regardless and does not care about wording at all.
    """
    start = max(0, position - _NEGATION_WINDOW)
    preceding = lowered[start:position]
    return any(re.search(rf"\b{re.escape(word)}\b", preceding) for word in _NEGATORS)


def _normalise_figure(text: str) -> str:
    """A figure reduced to a comparable form: digits only, trailing zeros dropped.

    So that "17,952.00", "17952.0" and "17952" are one value. Without this the check would
    flag a narrative for formatting an amount differently from the engine, which is a false
    positive that would train a reader to ignore the finding.
    """
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return text
    return str(value.normalize())


def supported_figures(values: list[str]) -> set[str]:
    """The comparable forms of every figure the engine computed.

    Takes strings rather than Decimals because the callers hold a mixture: amounts, limits,
    percentages, dates and record identifiers all end up as text in a recommendation.
    """
    supported: set[str] = set()
    for value in values:
        supported.add(value)
        supported.add(_normalise_figure(value))
        for match in _FIGURE.findall(value):
            supported.add(_normalise_figure(match))
        for match in _DATE.findall(value):
            supported.add(match)
    return supported


def _is_significant(figure: str) -> bool:
    """Whether a figure is worth checking.

    Small bare integers are skipped: "two approvals", "3 business days" and "the top 6 chunks"
    are counts the engine does not carry as values, and flagging them would bury the amounts
    that matter. Anything with a decimal point, a thousands separator, or four or more digits
    is treated as a figure a reader could act on.
    """
    if "," in figure or "." in figure:
        return True
    return len(figure) >= 4


def screen_narrative(
    summary: str,
    next_action: str,
    *,
    injection_codes: list[str],
    approval_is_recorded: bool,
    settlement_is_authorised: bool,
    computed_values: list[str],
) -> NarrativeScreenResult:
    """Screen approver-facing prose against the run's own state.

    ``approval_is_recorded`` is true once the signature requirement is met, and
    ``settlement_is_authorised`` once a decision has been recorded. Both are passed in rather
    than derived here so this function stays pure and testable: its whole value is that it can
    be run over adversarial strings in a unit test without an orchestrator, a model or a store.
    """
    blob = f"{summary}\n{next_action}"
    lowered = blob.lower()

    claims: list[str] = []
    if not approval_is_recorded:
        for match in _APPROVAL_CLAIM.finditer(lowered):
            if _is_negated(lowered, match.start()):
                continue
            claims.append(f"approval asserted: {match.group(0).strip()}")
    if not settlement_is_authorised:
        for settlement in _SETTLEMENT_WORDS:
            for match in re.finditer(rf"\b{re.escape(settlement)}\b", lowered):
                if _is_negated(lowered, match.start()):
                    continue
                window = lowered[match.start() : match.start() + _CLAIM_WINDOW]
                nearby = [word for word in _IMMEDIACY_WORDS if word in window]
                if nearby:
                    claims.append(f"immediate settlement asserted: {settlement} ... {nearby[0]}")
                    break

    supported = supported_figures(computed_values)
    unsupported = [
        figure
        for figure in _FIGURE.findall(blob)
        if _is_significant(figure) and _normalise_figure(figure) not in supported
    ]
    unsupported.extend(date for date in _DATE.findall(blob) if date not in supported)

    return NarrativeScreenResult(
        injection_codes=injection_codes,
        unsupported_claims=sorted(set(claims)),
        unsupported_figures=sorted(set(unsupported)),
    )
