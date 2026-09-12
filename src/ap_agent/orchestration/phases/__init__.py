"""The phase plan, and the retrieval queries each phase issues.

Separated from the machine so the plan can be read, and the tool budget derived, without
following control flow.

## Why tools are called conditionally

The brief asks for tools to be called only when needed rather than indiscriminately. With a
free-running agent loop that is a property one hopes the model exhibits. Here it is a
property of the plan, stated as rules:

``get_vendor_record``        always. FIN-POL-001 §5 makes confirming vendor status mandatory.
``check_invoice_history``    always. FIN-POL-005 §1 makes duplicate detection mandatory.
``get_purchase_order``       only when the case carries a purchase-order reference. Without
                             one there is no order to fetch, and a call would be a guess.
``retrieve_finance_documents``
                             four policy queries, always, because four independent controls
                             need citations; plus one evidence query, only when the case has
                             free text or the vendor shows a recent bank change, which are
                             the conditions under which supplier-supplied material is worth
                             searching for.
``get_authority_delegation`` only when an approval callback names a delegation. Fetching it
                             earlier would read the authority register for every run,
                             including the majority that never reach an approver.

The saving is real but secondary. The reason that matters is that each call has a stated
precondition, so a reviewer can tell whether a call was justified, and an injected document
cannot cause one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ap_agent.domain.enums import RunPhase

#: Execution order. ``AWAITING_APPROVAL`` and the terminal phases are reached by transition
#: rather than by falling through this list.
PHASE_ORDER: Final[tuple[RunPhase, ...]] = (
    RunPhase.INTAKE,
    RunPhase.RETRIEVE_POLICY,
    RunPhase.GATHER_EVIDENCE,
    RunPhase.RECONCILE,
    RunPhase.ASSESS_RISK,
    RunPhase.RECOMMEND,
)


def next_phase(current: RunPhase) -> RunPhase | None:
    """The phase that follows, or ``None`` at the end of the linear plan."""
    if current not in PHASE_ORDER:
        return None
    index = PHASE_ORDER.index(current)
    if index + 1 < len(PHASE_ORDER):
        return PHASE_ORDER[index + 1]
    return None


@dataclass(frozen=True)
class PolicyQuery:
    """A policy lookup with the control it supplies citations for."""

    purpose: str
    text: str
    top_k: int = 4


#: One query per control that needs a citation. Written the way an analyst would ask, not
#: copied from the target sections, so the retrieval measurement is honest.
POLICY_QUERIES: Final[tuple[PolicyQuery, ...]] = (
    PolicyQuery(
        purpose="three_way_match",
        text="three-way matching tolerance price variance quantity and goods receipt requirements",
    ),
    PolicyQuery(
        purpose="delegated_authority",
        text="delegated financial authority approval limits and when two approvals are required",
    ),
    PolicyQuery(
        purpose="duplicate_and_fraud",
        text="duplicate invoice detection matching fields and fraud indicators",
    ),
    PolicyQuery(
        purpose="vendor_controls",
        text="vendor status values requiring a hold and verifying a bank account change",
    ),
)

#: Issued only when the preconditions in the module docstring hold.
EVIDENCE_QUERY: Final = PolicyQuery(
    purpose="supplier_supplied_material",
    text="supplier payment instructions urgent bank account change new account details",
    top_k=4,
)


#: Worst-case tool attempts for one run, used to derive the default budget:
#:
#:   4  policy retrievals                     (1 attempt each in practice)
#:   1  evidence retrieval, conditional
#:   3  get_vendor_record                     (1 attempt plus 2 retries)
#:   3  get_purchase_order, conditional       (1 attempt plus 2 retries)
#:   3  check_invoice_history                 (1 attempt plus 2 retries)
#:   1  get_authority_delegation, conditional (at approval time)
#:   1  submit_finance_decision, no retries
#:  ---
#:   16
#:
#: The default is set to this figure rather than to a round number, so exhausting the budget
#: means something genuinely unexpected happened rather than that the estimate was low.
WORST_CASE_TOOL_ATTEMPTS: Final = 16

#: One step per phase in the linear plan, plus the approval stop, the decision execution and
#: two spare for a resume re-entering a phase.
WORST_CASE_STEPS: Final = 12
