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
| Decision reference | DEC-0ED8A5505975 |

## Recommendation

**Outcome:** `APPROVE_FOR_POSTING`

**Summary:** The invoice matches its purchase order and recorded receipts within policy tolerance, the vendor is active with no blocking flags, and no duplicate was found. Approval is required before posting.

**Next action:** Route to an approver holding at least the required delegated authority, then post and schedule for the next standard payment run.

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
| `required_approval_authority` | lowest role in the FIN-POL-003 §2 matrix whose maximum >= total_commitment | 50000 AUD | FIN-POL-003 §2 |  |

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

12 of 12 controls satisfied. Passing findings are retained deliberately: a record showing which controls were evaluated and satisfied is what distinguishes a run that checked everything from one that happened not to notice anything.

| Rule | Policy | Verdict | Detail |
|---|---|---|---|
| `purchase_order_approved` | FIN-POL-002 §1 | satisfied | purchase order PO-88121 is approved |
| `currency_matches_purchase_order` | FIN-POL-002 §1 | satisfied | invoice and order are both in AUD |
| `document_total_within_tolerance` | FIN-POL-002 §2 | satisfied | variance 0.00 AUD against limit 50.00 AUD |
| `receipt_recorded` | FIN-POL-002 §4 | satisfied | 2 receipt record(s) against PO-88121 |
| `no_duplicate_detected` | FIN-POL-005 §1 | satisfied | 2 candidate record(s) examined against paid, posted, held and rejected history; none match... |
| `vendor_status_active` | FIN-POL-004 §4 | satisfied | vendor V-1001 is ACTIVE |
| `bank_details_stable` | FIN-POL-004 §2 | satisfied | no bank-detail change inside the 30-day window |
| `segregation_of_duties` | FIN-POL-001 §4 | satisfied | requester and vendor creator are different people |
| `legal_name_agreement` | FIN-POL-004 §4 | satisfied | invoice vendor name matches the master record |
| `approval_authority_determined` | FIN-POL-003 §2 | satisfied | 17952.00 AUD requires at least DEPARTMENT_DIRECTOR (limit 50000 AUD) |
| `outcome_determined_deterministically` | FIN-POL-002 §5 | satisfied | The outcome was computed by the rule engine from typed facts. three-way match within toler... |
| `approver_within_authority` | FIN-POL-003 §5 | satisfied | U-3081 as DEPARTMENT_DIRECTOR may approve 17952.00 AUD against a limit of 50000 AUD, regis... |

## Actions taken

- **RECORD_APPROVE_FOR_POSTING** against `SIMULATED_ERP`
  - Reference: `DEC-0ED8A5505975`
  - Simulated: True
  - Authorised by U-3081 (DEPARTMENT_DIRECTOR); approval apr_589d08df023246de was granted by U-3081 (DEPARTMENT_DIRECTOR) for APPROVE_FOR_POSTING

## Decision receipt

```json
{
  "decision_ref": "DEC-0ED8A5505975",
  "run_id": "run_b45c118ce15446f4",
  "case_id": "FIN-001",
  "outcome": "APPROVE_FOR_POSTING",
  "amount": "17952.00",
  "currency": "AUD",
  "idempotency_key": "0ed8a5505975266d961c0fb516ce423bce90bd0c570aae0cd9263543620db0c6",
  "recorded_at": "2026-09-11T02:48:57.934835Z",
  "simulated": true,
  "replayed": false,
  "posting_system": "SIMULATED_ERP"
}
```

`simulated: true` and `posting_system: SIMULATED_ERP` are not decoration. There is no payment rail in this code path.

## Audit event log

36 events, in order. Every one carries a timestamp, the run and correlation identifiers, an outcome and a duration. Payloads are redacted at a single egress point before they are written.

| # | Event | Phase | Outcome | ms | Detail |
|---|---|---|---|---|---|
| 1 | `RUN_CREATED` | INTAKE |  |  | provider=fake; model=fake-deterministic-1 |
| 2 | `PHASE_STARTED` | INTAKE |  |  |  |
| 3 | `PHASE_COMPLETED` | INTAKE | SUCCESS | 1 | next_phase=RETRIEVE_POLICY |
| 4 | `PHASE_STARTED` | RETRIEVE_POLICY |  |  |  |
| 5 | `TOOL_CALL` |  | SUCCESS | 34 | tool=retrieve_finance_documents; attempt=1 |
| 6 | `RETRIEVAL` |  | SUCCESS | 37 | query=three-way matching tolerance price variance quantity and goods rece...; purpose=three_way_match; result_count=4 |
| 7 | `TOOL_CALL` |  | SUCCESS | 15 | tool=retrieve_finance_documents; attempt=1 |
| 8 | `RETRIEVAL` |  | SUCCESS | 16 | query=delegated financial authority approval limits and when two approval...; purpose=delegated_authority; result_count=4 |
| 9 | `TOOL_CALL` |  | SUCCESS | 20 | tool=retrieve_finance_documents; attempt=1 |
| 10 | `RETRIEVAL` |  | SUCCESS | 21 | query=duplicate invoice detection matching fields and fraud indicators; purpose=duplicate_and_fraud; result_count=4 |
| 11 | `TOOL_CALL` |  | SUCCESS | 10 | tool=retrieve_finance_documents; attempt=1 |
| 12 | `RETRIEVAL` |  | SUCCESS | 11 | query=vendor status values requiring a hold and verifying a bank account ...; purpose=vendor_controls; result_count=4 |
| 13 | `PHASE_COMPLETED` | RETRIEVE_POLICY | SUCCESS | 94 | next_phase=GATHER_EVIDENCE |
| 14 | `PHASE_STARTED` | GATHER_EVIDENCE |  |  |  |
| 15 | `TOOL_CALL` |  | SUCCESS | 21 | tool=get_vendor_record; attempt=1 |
| 16 | `TOOL_CALL` |  | SUCCESS | 5 | tool=get_purchase_order; attempt=1 |
| 17 | `TOOL_CALL` |  | SUCCESS | 4 | tool=check_invoice_history; attempt=1 |
| 18 | `PHASE_COMPLETED` | GATHER_EVIDENCE | SUCCESS | 40 | next_phase=RECONCILE |
| 19 | `PHASE_STARTED` | RECONCILE |  |  |  |
| 20 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=three_way_match; exception_count=0 |
| 21 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=duplicate_check; exception_count=0 |
| 22 | `RULE_EVALUATED` | RECONCILE | SUCCESS |  | rule_group=vendor_status_check; exception_count=0 |
| 23 | `PHASE_COMPLETED` | RECONCILE | SUCCESS | 6 | next_phase=ASSESS_RISK |
| 24 | `PHASE_STARTED` | ASSESS_RISK |  |  |  |
| 25 | `MODEL_CALL` |  | SUCCESS | 0 | provider=fake; model=fake-deterministic-1; schema=EvidenceSynthesis; attempt=1 |
| 26 | `PHASE_COMPLETED` | ASSESS_RISK | SUCCESS | 3 | next_phase=RECOMMEND |
| 27 | `PHASE_STARTED` | RECOMMEND |  |  |  |
| 28 | `MODEL_CALL` |  | SUCCESS | 0 | provider=fake; model=fake-deterministic-1; schema=RecommendationNarrative; attempt=1 |
| 29 | `RECOMMENDATION_READY` | RECOMMEND | APPROVE_FOR_POSTING |  | outcome=APPROVE_FOR_POSTING |
| 30 | `APPROVAL_REQUESTED` | RECOMMEND | AWAITING_HUMAN_DECISION |  | approval_id=apr_589d08df023246de; requested_outcome=APPROVE_FOR_POSTING |
| 31 | `PHASE_COMPLETED` | RECOMMEND | SUCCESS | 8 | next_phase=AWAITING_APPROVAL |
| 32 | `APPROVAL_RESOLVED` | AWAITING_APPROVAL | APPROVED |  | approval_id=apr_589d08df023246de |
| 33 | `PHASE_STARTED` | EXECUTE_DECISION |  |  |  |
| 34 | `DECISION_SUBMITTED` | EXECUTE_DECISION | RECORDED | 1 | outcome=APPROVE_FOR_POSTING; decision_ref=DEC-0ED8A5505975; replayed=False |
| 35 | `PHASE_COMPLETED` | EXECUTE_DECISION | SUCCESS | 4 | next_phase=COMPLETED |
| 36 | `RUN_COMPLETED` | COMPLETED | COMPLETED |  | outcome=APPROVE_FOR_POSTING; decision_ref=DEC-0ED8A5505975; exception_count=0; status=COMPLETED |

