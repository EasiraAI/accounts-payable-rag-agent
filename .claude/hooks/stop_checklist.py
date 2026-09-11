"""Stop hook: print the definition-of-done checklist when a turn ends.

Purely informational. It reminds whoever is working (human or agent) which gates the
brief requires before any claim of completion.
"""
import sys

CHECKLIST = """
--- definition of done (from CLAUDE.md §7 / §10) ---
[ ] pytest tests/unit tests/contract passes (no model calls)
[ ] python -m ap_agent.cli eval  -> FIN-001..FIN-005 all PASS
[ ] no secrets / bank numbers / personal data in tracked files
[ ] README + design note + manifest + diagram updated for any changed behaviour
[ ] sample transcripts regenerated if output shape changed
"""

sys.stderr.write(CHECKLIST)
sys.exit(0)
