# ADR-0007: Two-signature approvals, and where distinctness is enforced

**Status:** Accepted. **Date:** 2026-09-11. **Supersedes:** the single-approver assumption in
the first release of the README.

## Context

FIN-POL-003 §3 requires two approvals for a higher-risk transaction, and requires that "one
approver must be from Financial Control". The first release computed that requirement,
recorded it on the approval, presented it to the reader, and then resolved the approval on the
first callback. The requirement was reported and not enforced, which is the worst of the three
available positions: a reviewer reading the run output saw a control that the system did not
apply.

Enforcing it raises a question the policy does not answer directly. Approval callbacks arrive
over a network and are therefore delivered at least once. FIN-005 exists precisely because a
duplicate delivery must be safe. So when the same approver signs twice, the system has to
decide whether that is a second signature, an error, or a replay — and the honest answer
depends on something the callback cannot tell us, namely whether the sender meant to sign
again or simply retried.

## Options considered

| Option | Two approvals means | Duplicate delivery | Problem |
|---|---|---|---|
| Count callbacks | Two callbacks | Satisfies the requirement | One person clicking twice, or one retried delivery, releases the payment. Defeats the control entirely. |
| Count callbacks, reject a repeat signatory in the rules | Two callbacks from two people | Raises an exception | An ordinary at-least-once retry becomes a failed run. The caller's correct behaviour is punished. |
| **Store signatures, keyed by approver; require distinct signatories** | Two rows | Collides on the primary key and is reported as a replay | The requirement and the replay are answered by the same mechanism. |
| Require an idempotency token from the caller | Two tokens | Depends on the caller | Trusts a caller-supplied value with a control decision. Rejected for the same reason the decision tool refuses a caller-supplied key. |

## Decision

An approval accumulates signatures. `approval_signatures` has primary key
`(approval_id, approver_id)`, and the gate opens only when
`ApprovalRequest.signature_requirement_met` is true, which asks three things:

1. enough signatures have been collected for `required_signature_count`;
2. they come from that many *distinct* signatories;
3. if `requires_financial_control` is set, one of them is a Financial Control signature.

Distinctness is enforced by the primary key and by counting distinct signatories. It is
deliberately **not** checked in `domain/rules/segregation.py`, where an earlier version raised
an exception for a repeat signatory. `INSERT OR IGNORE` against that key returns a row count
of zero for a repeat, and that zero is exactly the replay signal the approval path already
knows how to report: the caller receives the same response body with `replayed: true`, no
second signature is recorded, and the gate stays shut.

Financial Control is allowed to co-sign without holding a monetary limit. FIN-POL-003 §2 gives
that role no limit, and §3 makes it the required second approver, so `validate_approval` takes
an `as_co_approver` flag that is set when a signature already on file satisfies the limit. The
limit is met by the primary approver; the oversight requirement is met by the co-approver.
Without the flag, the role the policy names as the second approver could never be one.

## Rationale

The requirement and the replay are the same question asked from two directions: has this
person already signed? A single mechanism that answers both cannot disagree with itself, and
putting that mechanism in the database rather than in rule code means it holds across
processes and survives a crash between reading the signature list and writing to it — the same
argument ADR-0004 makes for the decision table.

Raising an exception for a repeat signature was tried first and was wrong. It made a correct
client, one that retries a delivery it did not get a response to, produce a failed run.

## Consequences

- One defect was found by the tests rather than by reasoning: the branch that keeps the gate
  closed after a signature returned a hardcoded `False` for the replay flag, so a retried
  delivery from an approver who had already signed was reported to the caller as progress
  towards the second signature. The regression test asserts on the flag, not just on the
  stored state.
- `outstanding_requirement_detail()` states what is still missing, so a client polling the
  approval reads `1 further distinct signature(s) required (1 of 2 collected); one signature
  must come from Financial Control (FIN-POL-003 §3)` rather than an unexplained pending
  status.
- A second approval requires a second call. The CLI and the HTTP endpoint both accept it; the
  run stays `AWAITING_APPROVAL` in between, which is the state the resume path already
  handles.
- The signature rows carry the approver's effective role, applicable limit, authority-register
  version and whether a delegation was applied, which is what FIN-POL-003 §5 requires an
  approval record to identify. They are written once and never updated.
