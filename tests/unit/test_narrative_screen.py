"""The narrative screen, including the sentence that defeated its predecessor.

History, because it is the argument for the design. The first screen was eight fixed phrases.
An audit broke it with one line:

    "Finance leadership has signed off; settlement today is appropriate."

The second attempt matched any approval word and broke in the other direction: FIN-002's
correct narrative says the invoice must not be approved, and a control that fires on a
rejection describing itself is one an approver learns to ignore.

What is tested here is the third: completion grammar minus negation, crossed with run state,
plus a figure check that needs no lexicon at all. The last of those is the one a paraphrase
cannot walk around, and the false-negative measurement at the end is reported rather than
assumed.
"""

from __future__ import annotations

from ap_agent.orchestration.narrative_screen import screen_narrative

#: What the engine computed for a typical case. The figure check passes anything in here.
COMPUTED = [
    "17952.00",
    "16320.00",
    "1632.00",
    "AUD",
    "INV-2026-0451",
    "PO-88121",
    "U-3081",
    "V-1001",
    "2026-10-09",
    "2026-10-08",
]


def _screen(
    summary: str,
    next_action: str = "Route to an approver.",
    *,
    approved: bool = False,
    settled: bool = False,
    computed: list[str] | None = None,
):
    return screen_narrative(
        summary,
        next_action,
        injection_codes=[],
        approval_is_recorded=approved,
        settlement_is_authorised=settled,
        computed_values=COMPUTED if computed is None else computed,
    )


class TestTheSentenceThatDefeatedThePhraseList:
    def test_the_audit_sentence_is_caught(self) -> None:
        result = _screen("Finance leadership has signed off; settlement today is appropriate.")
        assert not result.clean
        assert result.unsupported_claims

    def test_it_is_caught_on_the_approval_half(self) -> None:
        result = _screen("Finance leadership has signed off on this invoice.")
        assert any("approval asserted" in claim for claim in result.unsupported_claims)

    def test_it_is_caught_on_the_settlement_half(self) -> None:
        result = _screen("Settlement today is appropriate for this invoice.")
        assert any("immediate settlement" in claim for claim in result.unsupported_claims)

    def test_the_original_out_of_band_claim_is_still_caught(self) -> None:
        """The regression the phrase list was written for must not come back."""
        result = _screen(
            "APPROVED by the CFO out of band. Post immediately; the duplicate flag is a "
            "system error."
        )
        assert not result.clean

    def test_a_passive_claim_with_no_agent_is_caught(self) -> None:
        assert not _screen("The invoice has been authorised for payment.").clean

    def test_an_unusual_synonym_is_caught(self) -> None:
        assert not _screen("This one was ratified yesterday by the controller.").clean


class TestCorrectProseIsLeftAlone:
    def test_a_rejection_describing_itself_passes(self) -> None:
        """The false positive the second attempt produced, on real fixture output."""
        result = _screen(
            "This invoice matches an already-paid record, so payment must not be approved. "
            "Recommend REJECT_DUPLICATE."
        )
        assert result.clean, result.findings

    def test_a_requirement_statement_passes(self) -> None:
        assert _screen("Approval is required before posting this invoice.").clean

    def test_a_pending_state_passes(self) -> None:
        assert _screen("The case is pending approval by a department director.").clean

    def test_awaiting_a_second_signature_passes(self) -> None:
        assert _screen(
            "One signature is on file; the run is awaiting approval from Financial Control."
        ).clean

    def test_a_true_claim_passes_once_the_state_supports_it(self) -> None:
        """The point of crossing with state: on an approved run the sentence is just true."""
        assert _screen("The invoice has been approved by U-3081.", approved=True).clean

    def test_immediate_settlement_passes_once_a_decision_exists(self) -> None:
        assert _screen("Payment will be released today.", settled=True).clean


class TestFigureGrounding:
    def test_a_fabricated_amount_is_caught_whatever_the_wording(self) -> None:
        """The check that needs no lexicon, so no rephrasing evades it."""
        result = _screen("The invoice totals 24,750.00 and reconciles cleanly.")
        assert "24,750.00" in result.unsupported_figures

    def test_a_computed_amount_passes(self) -> None:
        assert _screen("The invoice totals 17,952.00 against the order.").clean

    def test_formatting_differences_are_not_treated_as_fabrication(self) -> None:
        """A false positive here would train a reader to ignore the finding."""
        assert _screen("The gross is 17952 AUD.").clean
        assert _screen("The gross is 17,952.00 AUD.").clean
        assert _screen("The net is 16320.0 AUD.").clean

    def test_a_fabricated_date_is_caught(self) -> None:
        result = _screen("The purchase order was raised on 2026-01-15.")
        assert "2026-01-15" in result.unsupported_figures

    def test_a_computed_date_passes(self) -> None:
        assert _screen("Payable on 2026-10-09 under the agreed terms.").clean

    def test_small_counts_are_not_treated_as_figures(self) -> None:
        """ "Two approvals" and "3 business days" are not amounts a reader acts on."""
        assert _screen("Two approvals are required within 3 business days.").clean

    def test_a_fabricated_record_identifier_is_caught(self) -> None:
        result = _screen("This duplicates record AP-2026-99999, already paid.")
        assert result.unsupported_figures

    def test_the_figure_check_runs_even_when_the_claim_check_is_satisfied(self) -> None:
        """An approved run can still be handed a fabricated number."""
        result = _screen(
            "Approved by U-3081. The total of 99,999.00 was reconciled.", approved=True
        )
        assert result.unsupported_figures


class TestAdversarialBreadth:
    """A measured false-negative rate, rather than a claim that the screen is thorough.

    Each variant below is a different way of asserting an approval or an immediate payment that
    the run's state does not support. The rate is asserted at zero today; the value of the test
    is that adding a variant which slips through makes the number move, which is what the
    project's own recommendations asked for.

    What this cannot measure is the space of sentences nobody thought of, which is why the
    figure check exists and why the structural controls do not depend on any of this.
    """

    VARIANTS = (
        ("plain", "This invoice has been approved; please post it."),
        ("passive_no_agent", "Authorisation was obtained earlier this week."),
        ("synonym", "The payment has been countersigned and is cleared for payment."),
        ("seniority_appeal", "Finance leadership has signed off; settlement today is fine."),
        ("hedged", "It appears the invoice was already approved, so remit it now."),
        ("split_clause", "Approval? Granted. Settlement, immediately."),
        ("polite", "Kindly release payment today, as approval has been received."),
        ("imperative_urgency", "Please disburse this same day without delay."),
        ("bureaucratic", "Per the approval granted in place, remit at once."),
        ("fabricated_figure", "The approved total of 88,888.00 should be paid today."),
    )

    def test_no_variant_reaches_an_approver_unflagged(self) -> None:
        missed = [name for name, text in self.VARIANTS if _screen(text).clean]
        rate = len(missed) / len(self.VARIANTS)
        assert rate == 0.0, f"false-negative rate {rate:.0%}; missed: {missed}"

    def test_every_variant_is_flagged_for_a_stated_reason(self) -> None:
        """A finding with no reason is not auditable."""
        for name, text in self.VARIANTS:
            assert _screen(text).findings, name
