# Sample transcript: successful flow (FIN-001)

A clean three-way match. Invoice, purchase order and recorded receipts agree, the vendor is active with no blocking flags, and no duplicate exists. The run reaches `APPROVE_FOR_POSTING`, creates an approval request, and stops. Nothing is recorded until a human decides; the decision is then recorded exactly once.

Generated from the transcript the evaluation run emitted, not written by hand, so it cannot describe behaviour the tests did not observe. Source: [FIN-001_transcript.json](FIN-001_transcript.json). Regenerate with `uv run python scripts/render_samples.py`.

---

## Outcome

|  |  |
|---|---|
| Case | FIN-001 |
| Status | COMPLETED |
| Terminal phase | COMPLETED |
| Outcome | APPROVE_FOR_POSTING |
| Steps used | 7 |
| Tool attempts used | 7 |
| Exceptions raised | none |
| Fraud indicators | none |
| Unknowns | 0 |
| Decision reference | DEC-16BA58379A98 |

## Recommendation

**Outcome:** `APPROVE_FOR_POSTING`

**Summary:** The invoice matches its purchase order and recorded receipts within policy tolerance, the vendor is active with no blocking flags, and no duplicate was found. Approval is required before posting.

**Next action:** Route to an approver holding at least the required delegated authority, then post and schedule for the next standard payment run. Proposed payment run 2026-10-08, ahead of the due date 2026-10-09 (FIN-POL-006 §2). A proposal only: an agent may prepare a schedule and may not release a payment file.

**Confidence:** 0.88 — complete three-way match with recorded receipts and a clean vendor record

Drivers:

- purchase order and receipts both present
- all line and document variances inside tolerance
- no duplicate candidates matched

Limits:

- vendor and delegation data are read from simulated systems of record
- public-holiday calendar is not available, so due-date scheduling is indicative

**Requires approval:** True

## Calculations

Every figure below was computed in decimal arithmetic by the rule engine, not by the model (FIN-POL-002 §5). Inputs, formula, result and rounding are all stored so any of them can be re-performed by hand.

| Calculation | Formula | Result | Policy | Verdict |
|---|---|---|---|---|
| `invoice_internal_consistency` | invoice_net - sum(line_total) | 0.00 AUD | FIN-POL-001 §2 |  |
| `line_1_expected_value` | quantity_invoiced x po_unit_price | 11520.00 AUD | FIN-POL-002 §5 |  |
| `line_1_price_variance` | invoiced_value - expected_value | 0.00 AUD | FIN-POL-002 §5 |  |
| `line_1_tolerance_limit` | min(50.00, 1% of po_line_value) | 50.00 AUD | FIN-POL-002 §2 |  |
| `line_1_within_tolerance` | abs(variance) <= limit | 0.00 AUD | FIN-POL-002 §2 | pass |
| `line_1_quantity_check` | quantity_invoiced <= quantity_received | 0.00 | FIN-POL-002 §2 | pass |
| `line_2_expected_value` | quantity_invoiced x po_unit_price | 4800.00 AUD | FIN-POL-002 §5 |  |
| `line_2_price_variance` | invoiced_value - expected_value | 0.00 AUD | FIN-POL-002 §5 |  |
| `line_2_tolerance_limit` | min(50.00, 1% of po_line_value) | 48.00 AUD | FIN-POL-002 §2 |  |
| `line_2_within_tolerance` | abs(variance) <= limit | 0.00 AUD | FIN-POL-002 §2 | pass |
| `line_2_quantity_check` | quantity_invoiced <= quantity_received | 0.00 | FIN-POL-002 §2 | pass |
| `document_total_variance` | invoice_net - expected_net | 0.00 AUD | FIN-POL-002 §5 |  |
| `document_tolerance_limit` | max(per-line tolerance limits on the order) | 50.00 AUD | FIN-POL-002 §2 |  |
| `tax_expected_at_configured_rate` | invoice_net x rate_percent / 100 | 1632.00 AUD | FIN-POL-002 §5 |  |

(15 calculations in total.)

## Cited evidence

12 citations, each resolvable to one section of one document. The model returns chunk identifiers and they are resolved against what this run actually retrieved, so a fabricated citation cannot appear here.

| Chunk | Document | Section | Version | Status |
|---|---|---|---|---|
| `FIN-POL-002#2` | FIN-POL-002 | §2 | v2.4 | current |
| `FIN-POL-007#1` | FIN-POL-007 | §1 | v1.9 | current |
| `FIN-POL-001#5` | FIN-POL-001 | §5 | v3.2 | current |
| `FIN-POL-002#1` | FIN-POL-002 | §1 | v2.4 | current |
| `FIN-POL-003#3` | FIN-POL-003 | §3 | v4.0 | current |
| `FIN-POL-003#5` | FIN-POL-003 | §5 | v4.0 | current |
| `FIN-POL-001#3` | FIN-POL-001 | §3 | v3.2 | current |
| `FIN-POL-005#2` | FIN-POL-005 | §2 | v2.8 | current |
| `FIN-POL-008#5` | FIN-POL-008 | §5 | v2.1 | current |
| `FIN-POL-005#1` | FIN-POL-005 | §1 | v2.8 | current |
| `FIN-POL-004#2` | FIN-POL-004 | §2 | v5.1 | current |
| `FIN-POL-002#3` | FIN-POL-002 | §3 | v2.4 | current |

Note which documents are absent: `FIN-POL-003-OLD` (superseded), `ADV-001` (untrusted supplier notice) and `ADV-002` (irrelevant travel policy) are never cited as authority.

## Policy findings

19 of 19 controls satisfied. Passing findings are retained deliberately: a record showing which controls were evaluated and satisfied is what distinguishes a run that checked everything from one that happened not to notice anything.

| Rule | Policy | Verdict | Detail |
|---|---|---|---|
| `minimum_evidence_present` | FIN-POL-001 §2 | satisfied | present: supplier legal name, invoice number, currency, gross amount, invoice date, purcha... |
| `document_total_agrees_with_lines` | FIN-POL-001 §3 | satisfied | lines sum to 16320.00 AUD against a document net of 16320.00 AUD. Engine integrity check, ... |
| `purchase_order_approved` | FIN-POL-002 §1 | satisfied | purchase order PO-88121 is approved |
| `currency_matches_purchase_order` | FIN-POL-002 §1 | satisfied | invoice and order are both in AUD |
| `document_total_within_tolerance` | FIN-POL-002 §2 | satisfied | variance 0.00 AUD against limit 50.00 AUD |
| `receipt_recorded` | FIN-POL-002 §4 | satisfied | 2 receipt record(s) against PO-88121 |
| `no_duplicate_detected` | FIN-POL-005 §1 | satisfied | 2 candidate record(s) examined against paid, posted, held and rejected history; none match... |
| `vendor_status_active` | FIN-POL-004 §4 | satisfied | vendor V-1001 is ACTIVE |
| `first_payment_after_bank_change_co_approved` | FIN-POL-004 §2 | satisfied | bank details last changed on 2024-04-14, and a settled payment has followed, so the first-... |
| `legal_name_agreement` | FIN-POL-004 §1 | satisfied | invoice vendor name matches the master record |
| `payment_instructions_match_vendor_master` | FIN-POL-001 §5 | satisfied | No payment instructions were asserted in the case notes or attachments, so nothing contrad... |
| `segregation_of_duties_partial` | FIN-POL-001 §4 | satisfied | Nothing comparable at reconciliation: the invoice is at or below 25000 AUD, so the three-p... |
| `tax_assessed_separately` | FIN-POL-002 §2 | satisfied | stated tax 1632.00 AUD agrees with 10% of the net within 0.05 AUD |
| `agreed_payment_terms_applied` | FIN-POL-006 §1 | satisfied | 30 days from purchase order PO-88121 |
| `due_date_computed_from_agreed_terms` | FIN-POL-006 §1 | satisfied | 2026-09-09 plus 30 calendar days from purchase order PO-88121 gives 2026-10-09. Weekends o... |
| `scheduled_for_a_standard_payment_run` | FIN-POL-006 §2 | satisfied | proposed run 2026-10-08, the last standard run on or before 2026-10-09. Proposal only: und... |
| `approval_authority_determined` | FIN-POL-003 §2 | satisfied | 17952.00 AUD requires at least DEPARTMENT_DIRECTOR (limit 50000 AUD) |
| `outcome_determined_deterministically` | FIN-POL-002 §5 | satisfied | The outcome was computed by the rule engine from typed facts. three-way match within toler... |
| `approver_within_authority` | FIN-POL-003 §5 | satisfied | U-3081 as DEPARTMENT_DIRECTOR may approve 17952.00 AUD against a limit of 50000 AUD, regis... |

## Actions taken

- **RECORD_APPROVE_FOR_POSTING** against `SIMULATED_ERP`
  - Reference: `DEC-16BA58379A98`
  - Simulated: True
  - Authorised by U-3081 (DEPARTMENT_DIRECTOR); approval apr_e88d7c6089b64293 was granted by U-3081 (DEPARTMENT_DIRECTOR) for APPROVE_FOR_POSTING

## Decision receipt

```json
{
  "decision_ref": "DEC-16BA58379A98",
  "run_id": "run_f6a3b35c4a6d4331",
  "case_id": "FIN-001",
  "outcome": "APPROVE_FOR_POSTING",
  "amount": "17952.00",
  "currency": "AUD",
  "idempotency_key": "16ba58379a98770171ddd8c747c782acd7ed7a9d3a632620bbf078938bf5ac58",
  "recorded_at": "2026-09-12T04:06:15.814096Z",
  "simulated": true,
  "replayed": false,
  "posting_system": "SIMULATED_ERP"
}
```

`simulated: true` and `posting_system: SIMULATED_ERP` are not decoration. There is no payment rail in this code path.

## Audit event log

41 events, in order. Every one carries a timestamp, the run and correlation identifiers, an outcome and a duration. Payloads are redacted at a single egress point before they are written.

| # | Event | Phase | Outcome | ms | Detail |
|---|---|---|---|---|---|
| 1 | `RUN_CREATED` | INTAKE |  |  | provider=fake; model=fake-deterministic-1 |
| 2 | `PHASE_STARTED` | INTAKE |  |  |  |
| 3 | `PHASE_COMPLETED` | INTAKE | SUCCESS | 1 | next_phase=RETRIEVE_POLICY |
| 4 | `PHASE_STARTED` | RETRIEVE_POLICY |  |  |  |
| 5 | `TOOL_CALL` |  | SUCCESS | 1 | tool=retrieve_finance_documents; attempt=1 |
| 6 | `RETRIEVAL` |  | SUCCESS | 3 | query=three-way matching tolerance price variance quantity and goods rece...; purpose=three_way_match; result_count=4 |
| 7 | `TOOL_CALL` |  | SUCCESS | 0 | tool=retrieve_finance_documents; attempt=1 |
| 8 | `RETRIEVAL` |  | SUCCESS | 1 | query=delegated financial authority approval limits and when two approval...; purpose=delegated_authority; result_count=4 |
| 9 | `TOOL_CALL` |  | SUCCESS | 0 | tool=retrieve_finance_documents; attempt=1 |
| 10 | `RETRIEVAL` |  | SUCCESS | 1 | query=duplicate invoice detection matching fields and fraud indicators; purpose=duplicate_and_fraud; result_count=4 |
| 11 | `TOOL_CALL` |  | SUCCESS | 0 | tool=retrieve_finance_documents; attempt=1 |
| 12 | `RETRIEVAL` |  | SUCCESS | 1 | query=vendor status values requiring a hold and verifying a bank account ...; purpose=vendor_controls; result_count=4 |
| 13 | `PHASE_COMPLETED` | RETRIEVE_POLICY | SUCCESS | 12 | next_phase=GATHER_EVIDENCE |
| 14 | `PHASE_STARTED` | GATHER_EVIDENCE |  |  |  |
| 15 | `TOOL_CALL` |  | SUCCESS | 0 | tool=get_vendor_record; attempt=1 |
| 16 | `TOOL_CALL` |  | SUCCESS | 0 | tool=get_purchase_order; attempt=1 |
| 17 | `TOOL_CALL` |  | SUCCESS | 0 | tool=check_invoice_history; attempt=1 |
| 18 | `PHASE_COMPLETED` | GATHER_EVIDENCE | SUCCESS | 7 | next_phase=RECONCILE |
| 19 | `PHASE_STARTED` | RECONCILE |  |  |  |
| 20 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=three_way_match; exception_count=0 |
| 21 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=duplicate_check; exception_count=0 |
| 22 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=vendor_status_check; exception_count=0 |
| 23 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=payment_instructions; exception_count=0 |
| 24 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=segregation_of_duties; exception_count=0 |
| 25 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=tax_assessment; exception_count=0 |
| 26 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=payment_terms; exception_count=0 |
| 27 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=repeated_non_po; exception_count=0 |
| 28 | `PHASE_COMPLETED` | RECONCILE | SUCCESS | 7 | next_phase=ASSESS_RISK |
| 29 | `PHASE_STARTED` | ASSESS_RISK |  |  |  |
| 30 | `MODEL_CALL` |  | SUCCESS | 0 | provider=fake; model=fake-deterministic-1; schema=EvidenceSynthesis; attempt=1 |
| 31 | `PHASE_COMPLETED` | ASSESS_RISK | SUCCESS | 2 | next_phase=RECOMMEND |
| 32 | `PHASE_STARTED` | RECOMMEND |  |  |  |
| 33 | `MODEL_CALL` |  | SUCCESS | 0 | provider=fake; model=fake-deterministic-1; schema=RecommendationNarrative; attempt=1 |
| 34 | `RECOMMENDATION_READY` | RECOMMEND | APPROVE_FOR_POSTING |  | outcome=APPROVE_FOR_POSTING |
| 35 | `APPROVAL_REQUESTED` | RECOMMEND | AWAITING_HUMAN_DECISION |  | approval_id=apr_e88d7c6089b64293; requested_outcome=APPROVE_FOR_POSTING |
| 36 | `PHASE_COMPLETED` | RECOMMEND | SUCCESS | 5 | next_phase=AWAITING_APPROVAL |
| 37 | `APPROVAL_RESOLVED` | AWAITING_APPROVAL | APPROVED |  | approval_id=apr_e88d7c6089b64293 |
| 38 | `PHASE_STARTED` | EXECUTE_DECISION |  |  |  |
| 39 | `DECISION_SUBMITTED` | EXECUTE_DECISION | RECORDED | 0 | outcome=APPROVE_FOR_POSTING; decision_ref=DEC-16BA58379A98; replayed=False |
| 40 | `PHASE_COMPLETED` | EXECUTE_DECISION | SUCCESS | 3 | next_phase=COMPLETED |
| 41 | `RUN_COMPLETED` | COMPLETED | COMPLETED |  | outcome=APPROVE_FOR_POSTING; decision_ref=DEC-16BA58379A98; exception_count=0; status=COMPLETED |

