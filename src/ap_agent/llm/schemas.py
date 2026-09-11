"""Schemas the model is allowed to produce.

The model is confined to two jobs: reading retrieved text into structured statements, and
writing the narrative that goes to an approver. It never produces an outcome, an amount, or a
citation it invents.

## Citations are identifiers, not objects

The model returns ``citation_chunk_ids``, a list of chunk identifiers, never a citation
object. The orchestrator resolves each identifier against the chunks actually retrieved in
this run and discards anything it does not recognise, recording the discard as an event.

This is what makes grounding a property of the system rather than a hope about the model. A
model that invents "FIN-POL-002 §9" produces an identifier that resolves to nothing, so the
fabricated citation cannot reach the recommendation. Accepting a citation object from the
model would mean trusting its document identifier, version and quote, all three of which it
could get wrong or make up.

## The model may tighten an outcome, never loosen it

``suggested_outcome`` exists so a model that spots something the rule engine missed can push
the case towards caution. The orchestrator applies it only when it is *more* severe than the
computed outcome. A model asking to approve a held invoice is recorded and ignored; a model
asking to escalate an approved one is honoured. The asymmetry is the point: the failure mode
worth guarding against is an over-permissive model, and an injected instruction can only ever
argue for permission.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import Outcome

ShortText = Annotated[str, Field(min_length=1, max_length=600)]
Sentence = Annotated[str, Field(min_length=1, max_length=1_200)]

#: Severity order used to decide whether a model's suggestion tightens or loosens the
#: computed outcome. Higher is more conservative.
OUTCOME_SEVERITY: dict[Outcome, int] = {
    Outcome.APPROVE_FOR_POSTING: 0,
    Outcome.HOLD_FOR_INFORMATION: 1,
    Outcome.REJECT_INVALID: 2,
    Outcome.REJECT_DUPLICATE: 3,
    Outcome.ESCALATE_CONTROL_REVIEW: 4,
}


class ModelFact(BaseModel):
    """A statement the model read out of retrieved text."""

    model_config = ConfigDict(extra="forbid")

    statement: ShortText
    value: Annotated[str, Field(max_length=300)] = ""
    citation_chunk_ids: list[str] = Field(default_factory=list, max_length=8)


class ModelInference(BaseModel):
    """A conclusion the model drew that no single source states outright."""

    model_config = ConfigDict(extra="forbid")

    statement: ShortText
    basis: ShortText
    confidence: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.5")
    citation_chunk_ids: list[str] = Field(default_factory=list, max_length=8)


class ModelUnknown(BaseModel):
    """Something the model could not establish from the evidence it was given."""

    model_config = ConfigDict(extra="forbid")

    item: ShortText
    reason: ShortText
    impact: ShortText
    how_to_resolve: Annotated[str, Field(max_length=600)] = ""


class EvidenceSynthesis(BaseModel):
    """Output of the risk-assessment phase.

    ``injection_observed`` asks the model to report instruction-like content it noticed. The
    deterministic detector is the control; this is a second, independent observer. A
    disagreement between them is informative in either direction: the detector missing
    something the model saw is a gap in the patterns, and the model reporting nothing where
    the detector fired is a reason to distrust the model's reading of that document.
    """

    model_config = ConfigDict(extra="forbid")

    sourced_facts: list[ModelFact] = Field(default_factory=list, max_length=25)
    inferences: list[ModelInference] = Field(default_factory=list, max_length=15)
    unknowns: list[ModelUnknown] = Field(default_factory=list, max_length=15)
    injection_observed: bool = False
    injection_note: Annotated[str, Field(max_length=600)] = ""


class ModelConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: Annotated[Decimal, Field(ge=0, le=1)]
    basis: ShortText
    drivers: list[ShortText] = Field(default_factory=list, max_length=8)
    limits: list[ShortText] = Field(default_factory=list, max_length=8)


class RecommendationNarrative(BaseModel):
    """Output of the recommendation phase: prose and judgement, never the decision."""

    model_config = ConfigDict(extra="forbid")

    summary: Sentence
    assumptions: list[ShortText] = Field(default_factory=list, max_length=12)
    confidence: ModelConfidence
    next_action: Sentence
    #: May tighten the computed outcome, never loosen it. See the module docstring.
    suggested_outcome: Outcome | None = None
    suggested_outcome_reason: Annotated[str, Field(max_length=600)] = ""


def tightens(suggested: Outcome, computed: Outcome) -> bool:
    """Whether a suggested outcome is strictly more conservative than the computed one."""
    return OUTCOME_SEVERITY[suggested] > OUTCOME_SEVERITY[computed]
