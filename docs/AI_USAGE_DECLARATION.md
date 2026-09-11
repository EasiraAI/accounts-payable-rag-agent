# Declaration of AI assistance

This project was built with substantial use of AI coding assistance. This document says where,
how, and what was done to make the result trustworthy, because a declaration that says only
"AI was used" tells a reviewer nothing they can act on.

It is the only document in this repository that discusses the authoring process. Everything
else describes the system.

**Time spent: about 5 hours** against the brief's 8-hour timebox. The git history runs from
11:22 to 15:33 on 11 September 2026; reading the corpus and planning the design came before the
first commit. The assistance is the reason that number is what it is, which is the honest way
to read both figures together.

## Scope

AI assistance was used across the whole of the work: reading the policy corpus, weighing the
architectural options recorded in the decision records, writing the source and the tests,
writing the documentation, and — most usefully — reviewing all of it adversarially.

The exceptions are worth naming precisely, because they are where the assistance could not
help:

- **The brief and the policy corpus** are given inputs. Neither was generated here.
- **The decisions themselves.** Options were explored with assistance and the choice recorded
  in an ADR was a judgement call each time. The ADRs state the alternative that was rejected
  and why, so a reader can disagree with the choice on its merits rather than on its origin.
- **What counts as done.** Every number in the documentation reproduces from a command, and
  that standard was applied deliberately, precisely because prose about a system is the part
  most easily written faster than it can be checked.

## How it was used, and how the output was controlled

Three habits did most of the work of making the output reliable.

**Nothing was accepted because it looked right.** Every rule has tests at its boundaries, the
arithmetic has property-based tests, and the five fixture cases run end to end on a
deterministic model adapter. A change was not finished until `pytest`, `mypy --strict`, `ruff`
and `ap-agent eval` all passed, and the counts in the documentation were taken from those runs
rather than written from memory.

**The system was reviewed adversarially, more than once, by reviewers that had not written
it.** Five independent reviews were run against the finished code, each asked a single hostile
question — assume the model is hostile and the corpus is planted, what can they cause; does
every threshold and citation match the policy text; what fails silently; where does untrusted
input reach a decision; do the tests verify what they claim. Those reviews found four release
blockers and sixteen further defects in work that passed its own tests and looked complete.
The most serious were:

- a migration that could leave the database permanently unopenable after a badly timed crash;
- an assumed tax rate, nowhere stated in the policy, that could hold a valid invoice;
- a request-model default that rejected a legitimate invoice for not adding up to a figure the
  supplier never claimed;
- a duplicate-detection signal that held every corrected resubmission, the one case the policy
  explicitly warns against.

Each was fixed at the root, with a test that fails without the fix, and the reasoning recorded
in the code where the next reader will meet it. The git history shows the sequence rather than
a single tidy commit, because the sequence is the honest record.

**Claims were checked against the source, not against memory.** A documentation review
cross-referenced every statement in the README, the design note, the manifest and the seven
decision records against the code. It found thirty-one stale or contradicted statements,
including two that presented an enforced control as unenforced and one that described the
retrieval index as a pickle when the code deliberately uses JSON. Documentation drifts from
code in every project; assistance makes it drift faster, because prose is cheap to produce and
no cheaper to verify.

## What this means for a reviewer

The honest summary is that AI assistance changed the shape of the effort rather than removing
it. It made the first draft of everything faster and it made the *review* dramatically more
thorough, which is where the value actually landed: the defects listed above are ones that a
single pass over one's own work reliably misses, and several of them were confirmed by
reproducing the failure before the fix was written.

What it did not change is the standard. The controls trace to policy sections; the citations
were checked against the corpus text and corrected where they claimed more than the section
says; the safety properties are enforced structurally and asserted by tests; and the known
limitations are stated rather than omitted. Where a judgement is an interpretation of the
policy rather than its wording — the document-total tolerance, the choice of the latest
qualifying payment run, the two integrity checks that no clause prescribes — the code and the
documentation say so.

Responsibility for what is in this repository, including its remaining limitations, rests with
me as its author. The known ones are listed in [README §10](../README.md) and the work I would
do next is in [RECOMMENDATIONS.md](RECOMMENDATIONS.md), in the order I would do it.

## Tooling

Claude (Anthropic), used through an agentic coding environment with repository-scoped
configuration: task-specific review agents, reference notes carrying the policy thresholds and
the trust-boundary rules, and pre-write checks that refuse a commit containing a
credential-shaped or unmasked-account-shaped string. That configuration lives in `.claude/` and
is part of the repository rather than a local setup, so the review gates described above are
reproducible by anyone who opens it.

The `CLAUDE.md` at the repository root is the working specification for that environment: the
brief's requirements, the binding architecture decisions, and the delivered state. It is
addressed to whoever picks the work up next, human or otherwise.
