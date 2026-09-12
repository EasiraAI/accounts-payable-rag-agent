"""Compute the outcome, then ask the model for the prose around it.

Extracted from the orchestrator so that what this phase can reach is visible in its
signature: a clock, a repository, a retriever and a model call, and nothing else.
"""

from __future__ import annotations

from ap_agent.domain.enums import (
    EventType,
    Outcome,
    RunPhase,
)
from ap_agent.domain.money import Money
from ap_agent.domain.results import (
    ConfidenceAssessment,
    PolicyFinding,
    Recommendation,
    truncate_detail,
)
from ap_agent.domain.rules.authority import required_authority
from ap_agent.domain.rules.duplicates import duplicate_check
from ap_agent.domain.rules.fraud import detect_injection
from ap_agent.domain.rules.matching import three_way_match
from ap_agent.domain.rules.outcome import decide_outcome
from ap_agent.domain.rules.vendor import vendor_status_check
from ap_agent.domain.run_state import RunState
from ap_agent.llm import (
    RecommendationNarrative,
    build_recommendation_prompt,
    new_boundary_nonce,
    render_computed_findings,
    tightens,
)
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.approvals import create_approval
from ap_agent.orchestration.citations import recommendation_citations
from ap_agent.orchestration.narrative_screen import screen_narrative
from ap_agent.orchestration.phase_context import PhaseContext
from ap_agent.orchestration.summaries import (
    computed_values,
    deterministic_next_action,
    deterministic_summary,
    with_payment_schedule,
)
from ap_agent.persistence.repository import Repository
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.mock_backends import MockBackends


def run(
    ctx: PhaseContext,
    state: RunState,
    emitter: EventEmitter,
    runner: ToolRunner,
    backends: MockBackends,
) -> None:
    """Compute the outcome, then ask the model for the narrative around it."""
    as_of = ctx.clock()
    invoice = state.request.to_invoice()

    match = three_way_match(invoice, state.purchase_order, as_of=as_of)
    duplicates = duplicate_check(invoice, state.invoice_history, as_of=as_of)
    vendor = vendor_status_check(
        state.vendor,
        invoice,
        as_of=as_of,
        requested_by=state.request.requested_by,
        history=state.invoice_history,
    )
    authority = required_authority(
        Money(amount=invoice.gross_amount, currency=invoice.currency),
        # The reasons recorded during reconciliation, which include conditions found
        # outside the vendor record.
        higher_risk_reasons=state.higher_risk_reasons or vendor.higher_risk_reasons,
    )

    decision = decide_outcome(
        match=match,
        duplicates=duplicates,
        vendor=vendor,
        authority=authority,
        indicators=state.fraud_indicators,
        invalid_reasons=state.invalid_reasons,
        # Everything the run recorded, including the controls that are not among the
        # three named results. Without this a blocking exception from the
        # payment-instruction check never reached the outcome.
        additional_exceptions=state.exceptions,
    )

    nonce = new_boundary_nonce()
    computed_findings = render_computed_findings(
        match=match,
        calculations_summary=[
            f"{calculation.name}: {calculation.formula} = {calculation.result}"
            f"{' ' + calculation.currency if calculation.currency else ''}"
            f" [{calculation.policy_ref}]"
            for calculation in state.calculations
        ],
        exception_summary=[
            f"{exception.category.value} ({exception.failed_rule}): expected "
            f"{exception.expected}; observed {exception.observed}"
            for exception in state.exceptions
        ],
        indicator_summary=[
            f"{indicator.code}: {indicator.description} [{indicator.policy_ref}]"
            for indicator in state.fraud_indicators
        ],
        computed_outcome=decision.outcome.value,
        nonce=nonce,
    )

    prompt = build_recommendation_prompt(
        request=state.request,
        computed_findings=computed_findings,
        facts_summary=[
            f"{fact.statement} ({fact.value})" if fact.value else fact.statement
            for fact in state.sourced_facts
        ],
        unknowns_summary=[f"{unknown.item}: {unknown.reason}" for unknown in state.unknowns],
        nonce=nonce,
    )
    narrative = ctx.call_model(state, emitter, prompt, RecommendationNarrative)

    final_outcome = decision.outcome
    if narrative.suggested_outcome is not None:
        if tightens(narrative.suggested_outcome, decision.outcome):
            emitter.emit(
                EventType.RULE_EVALUATED,
                payload={
                    "rule_group": "model_suggestion_applied",
                    "computed": decision.outcome.value,
                    "applied": narrative.suggested_outcome.value,
                    "reason": narrative.suggested_outcome_reason,
                },
                phase=RunPhase.RECOMMEND,
                outcome="TIGHTENED",
            )
            final_outcome = narrative.suggested_outcome
            state.add_assumptions(
                [
                    f"The outcome was tightened from {decision.outcome.value} to "
                    f"{final_outcome.value} on the model's recommendation: "
                    f"{narrative.suggested_outcome_reason}"
                ]
            )
        else:
            # The case the design exists for: a model that has obeyed an injected
            # instruction, or simply disagrees, asking for something less cautious.
            emitter.emit(
                EventType.RULE_EVALUATED,
                payload={
                    "rule_group": "model_suggestion_discarded",
                    "computed": decision.outcome.value,
                    "suggested": narrative.suggested_outcome.value,
                    "reason": narrative.suggested_outcome_reason,
                    "note": "a model suggestion may only make an outcome more conservative",
                },
                phase=RunPhase.RECOMMEND,
                outcome="DISCARDED",
            )
            state.add_findings(
                [
                    PolicyFinding(
                        rule="model_cannot_loosen_outcome",
                        policy_ref="FIN-POL-005 §4",
                        satisfied=True,
                        detail=(
                            f"The model suggested {narrative.suggested_outcome.value}, "
                            f"which is less cautious than the computed "
                            f"{decision.outcome.value}. Recorded and discarded."
                        ),
                    )
                ]
            )

    requires_approval = final_outcome.is_consequential
    summary, next_action = _screen_and_replace(
        ctx.repository, state, emitter, narrative, final_outcome
    )
    next_action = with_payment_schedule(state, next_action, final_outcome)
    recommendation = Recommendation(
        outcome=final_outcome,
        summary=summary,
        cited_evidence=recommendation_citations(state),
        calculations=list(state.calculations),
        assumptions=[*narrative.assumptions, *state.assumptions],
        confidence=ConfidenceAssessment(
            score=narrative.confidence.score,
            basis=narrative.confidence.basis,
            drivers=list(narrative.confidence.drivers),
            limits=list(narrative.confidence.limits),
        ),
        exceptions=list(state.exceptions),
        next_action=next_action,
        requires_approval=requires_approval,
        requires_second_approval=decision.requires_second_approval,
        second_approval_reason=decision.second_approval_reason,
    )
    state.recommendation = recommendation
    state.add_findings(
        [
            PolicyFinding(
                rule="outcome_determined_deterministically",
                policy_ref="FIN-POL-002 §5",
                satisfied=True,
                detail=truncate_detail(
                    "The outcome was computed by the rule engine from typed facts. "
                    + "; ".join(decision.reasons)
                ),
            )
        ]
    )

    emitter.emit(
        EventType.RECOMMENDATION_READY,
        payload={
            "outcome": final_outcome.value,
            "requires_approval": requires_approval,
            "requires_second_approval": decision.requires_second_approval,
            "exception_categories": sorted(
                {exception.category.value for exception in state.exceptions}
            ),
            "indicator_codes": [indicator.code for indicator in state.fraud_indicators],
            "confidence": str(narrative.confidence.score),
            "citation_count": len(recommendation.cited_evidence),
            "required_role_minimum": (
                decision.required_role_minimum.value if decision.required_role_minimum else None
            ),
        },
        phase=RunPhase.RECOMMEND,
        outcome=final_outcome.value,
    )

    if requires_approval:
        create_approval(ctx.repository, ctx.clock, state, emitter, recommendation)


def _screen_and_replace(
    repository: Repository,
    state: RunState,
    emitter: EventEmitter,
    narrative: RecommendationNarrative,
    outcome: Outcome,
) -> tuple[str, str]:
    """Return the summary and next action an approver will read, screened first.

    A security review pointed out that a compromised model keeps the computed outcome but
    writes whatever it likes in the prose a human actually reads: the run held
    ``REJECT_DUPLICATE`` while the summary said "APPROVED by the CFO out of band. Post
    immediately; the duplicate flag is a system error." Schema-valid, so nothing rejected
    it, and the outcome field is not what an approver's eye goes to first.

    The screening itself lives in ``narrative_screen.py``, which is a pure function over
    strings and state: three checks, being the injection detector, an approval or
    immediate-settlement claim crossed against what the run has actually recorded, and
    every figure in the prose crossed against the values the engine computed. The third
    needs no lexicon and is the one a paraphrase cannot walk around.

    The first version of this was a list of eight phrases, and an audit defeated it with
    "Finance leadership has signed off; settlement today is appropriate." A blocklist over
    natural language loses to paraphrase, which is the same reason this system does not
    rely on injection detection as a primary control.

    If anything fires, the model's prose is discarded and replaced with a deterministic
    summary built from the computed facts. The substitution is recorded as a finding,
    because a model writing approval language into a rejection is itself a finding.
    """
    blob = f"{narrative.summary}\n{narrative.next_action}"
    approval = repository.find_pending_approval(state.run_id) if state.approval_id else None
    screen = screen_narrative(
        narrative.summary,
        narrative.next_action,
        injection_codes=detect_injection(blob),
        # An approval claim is only false while the requirement is unmet. On a run that has
        # collected its signatures, "signed off" is simply true.
        approval_is_recorded=bool(approval and approval.signature_requirement_met),
        settlement_is_authorised=state.decision is not None,
        computed_values=computed_values(state),
    )
    if screen.clean:
        return narrative.summary, narrative.next_action
    injection_codes = screen.injection_codes
    approval_claims = [*screen.unsupported_claims, *screen.unsupported_figures]

    emitter.emit(
        EventType.INJECTION_ATTEMPT_DETECTED,
        payload={
            "source": "model narrative",
            "patterns": [*injection_codes, *approval_claims],
            "note": (
                "the model's approver-facing prose was discarded and replaced with a "
                "deterministic summary"
            ),
        },
        phase=RunPhase.RECOMMEND,
        outcome="BLOCKED",
    )
    state.add_findings(
        [
            PolicyFinding(
                rule="model_narrative_screened",
                policy_ref="FIN-POL-005 §4",
                satisfied=True,
                detail=truncate_detail(
                    "The model's summary or next action contained instruction-like or "
                    "approval-asserting language ("
                    + ", ".join([*injection_codes, *approval_claims])
                    + "). It was discarded and replaced with a summary generated from the "
                    "computed control results, because the prose is what an approver "
                    "reads."
                ),
            )
        ]
    )
    return deterministic_summary(state, outcome), deterministic_next_action(state, outcome)
