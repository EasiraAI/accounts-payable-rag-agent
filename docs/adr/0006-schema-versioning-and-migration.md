# ADR-0006: Schema versioning and forward migration

**Status:** Accepted. **Date:** 2026-09-11. **Extends:** ADR-0004.

## Context

ADR-0004 chose SQLite and made the schema the enforcement point for the exactly-once
guarantee: a duplicated approval callback collides on a primary key rather than being caught
by application code, so the guarantee survives a crash between a check and a write.

That decision has a consequence ADR-0004 did not record. If the constraints are the
guarantee, then a store whose constraints differ from the code's expectations is not a
degraded system, it is an unsafe one.

The schema was applied by running `schema.sql` at connection time. Every statement in it is
`CREATE ... IF NOT EXISTS`, which is correct for an empty database and silently wrong for an
existing one: a table that already exists keeps the columns it was created with, and the
statement reports success.

This was found by adding `decisions.invoice_fingerprint` and its partial unique index, which
close the cross-run double-payment path. Against a development store created before the
column existed:

- the `CREATE TABLE IF NOT EXISTS` was a no-op, so the column was absent;
- the `CREATE UNIQUE INDEX` then failed with `no such column: invoice_fingerprint`;
- and the failure surfaced when the process next opened the database, not when the change
  was made.

A worse variant is available. Had the new object been a constraint on an existing column
rather than a new column, nothing would have failed at all. The store would have opened
cleanly, the code would have believed a uniqueness constraint was enforcing one decision per
invoice, and no such constraint would exist. The only symptom would be a duplicate payment.

## Options considered

| Option | Detects a stale store | Repairs it | Risk |
|---|---|---|---|
| **`user_version` pragma plus ordered migrations** | Yes, on open | Yes | Migration list must be maintained by hand. |
| Recreate the database when the shape differs | Yes | Discards every recorded decision and audit event | Unacceptable: the audit trail is a deliverable under FIN-POL-001 §6. |
| Compare `sqlite_master` against the expected DDL | Yes | No, only reports | Needs a canonical DDL string to diff against, and SQLite rewrites whitespace. |
| A migration framework (Alembic, yoyo) | Yes | Yes | A dependency and a migration runner for one table; the project has no ORM to hang it on. |
| Document "delete the state file after an upgrade" | No | By hand, destructively | Relies on a person reading a note, which is how the defect reached a running process. |

## Decision

The store carries its shape in SQLite's `user_version` pragma, and opening it is what brings
it up to date.

- `SCHEMA_VERSION` in `persistence/repository.py` is the shape this build expects.
- `_MIGRATIONS` maps each version to the statements that produce it from the one before.
  Migrations are written by hand rather than derived from `schema.sql`, because a migration
  has to say what happens to rows that already exist and a `CREATE` statement cannot express
  that. The `invoice_fingerprint` migration defaults existing rows to the empty string, which
  is the value the partial unique index excludes, so decisions recorded before the column
  existed do not collide with each other.
- Migrations run **before** `schema.sql`, because a statement in the script can depend on a
  column a migration adds. Migrations only alter tables that already exist, so the reverse
  dependency does not arise.
- An empty file is a fresh install and reports the current version, so a new database is
  created by the script and never migrated. A populated store whose pragma is still `0`
  predates versioning and is treated as version 1.
- A store written by a **newer** build is refused, with an error naming both versions. This is
  the asymmetry worth keeping: forward migration is a repair, but running older code against a
  forward-migrated store is how a constraint gets dropped from the enforcement path while the
  application still believes it is there.

## Rationale

The pragma is a two-byte integer in the database header that SQLite itself never reads. It
costs nothing, needs no table, and is the conventional place for exactly this. Given that the
schema is the safety mechanism, the version of the schema is safety-relevant state, and
safety-relevant state belongs in the store rather than in a deployment note.

Refusing a newer store is a deliberate choice to fail loudly on a rollback. A rollback that
keeps serving is the dangerous outcome here, because the decision table's constraints are
invisible from the application's side: code cannot tell whether its `INSERT OR IGNORE`
collided because a constraint held or because a constraint was absent and there was simply no
matching row.

## Consequences

- Adding a column or constraint now means adding a migration and incrementing
  `SCHEMA_VERSION`. Forgetting to is caught by the migration tests, which build a version-1
  database from raw SQL and assert that opening it produces the current shape.
- `Repository.schema_version` and `Repository.column_names` expose the store's shape, so an
  operator can assert on it and the tests do not reach into the connection.
- Migrations are not transactional as a group. Each statement commits, and the version is
  advanced after each step, so an interrupted upgrade resumes from the step it reached rather
  than re-running the ones that succeeded. SQLite's `ALTER TABLE ADD COLUMN` is a header
  rewrite and is itself atomic.
- There is no down migration. A rollback means restoring a backup taken before the upgrade,
  which is the honest position for a store whose contents are an audit record.
- PostgreSQL has no `user_version`; the same protocol there would be a one-row
  `schema_version` table read in the same place. ADR-0004's portability note still holds.
