---
name: engineering-voice
description: Writing style for every deliverable in this repo (README, design note, ADRs, docstrings, commit messages, sample transcripts). Use whenever producing or editing prose destined for the reviewer.
---

# Engineering voice

The repository reads as the work of a solution architect who researched the options and
built the system. Write accordingly.

## Do
- Lead with the decision, then the reason, then the alternatives rejected.
- Use first person plural sparingly ("we chose") or neutral voice ("the service does").
- Cite sources for non-obvious claims: papers, standards, vendor docs (see docs/references.md).
- State limitations plainly in a "Known limitations" section, not as apologies.
- Prefer tables for comparisons, numbered lists for procedures, prose for rationale.
- Keep commit messages conventional: `feat(rag): section-aware chunker with metadata`.

## Do not
- Do not describe the tooling used to write the code or docs. No mention of assistants,
  models used for authoring, generated-by notes, or session narration anywhere in the
  repo. The single exception is `docs/AI_USAGE_DECLARATION.md`, written last.
- Do not narrate process ("first I explored, then I decided"). Present conclusions.
- No em-dashes in prose. No emoji. No marketing adjectives.
- No hedged claims about test results. Either they pass with output shown, or they do not.

## Structure of the design note
1. Context and goals. 2. Orchestration. 3. RAG design and limitations. 4. Trust
boundaries. 5. Contracts. 6. Persistence and resume. 7. Failure handling.
8. Evaluation. 9. Production changes. 10. Enterprise scale. 11. Recommendations.
