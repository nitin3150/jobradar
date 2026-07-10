"""add ``paused`` to ``job_status`` enum — v0.6 operator-veto path.

Why this migration exists
========================

After the v0.6 single-threshold scoring flip, every AI-flagged winner
goes straight to ``status='approved'`` — the same pool the future
``apply_worker`` dequeues from via ``SELECT ... WHERE status='approved'
FOR UPDATE SKIP LOCKED``. Without an operator veto path, the worker
silently applies to every row the LLM scored above threshold, even
ones the operator spots have a deal-breaker (relocation, equity, visa,
comp floor) at-a-glance.

This migration adds ``paused`` to the canonical lifecycle:

    in_review → approved → paused → approved      (operator un-parked)
                          → rejected             (operator gave up)
                          → applied              (operator hand-applied)
                          → applied              (apply_worker never reaches here — guard)

The enum lives at the Postgres layer; adding a value requires this
migration so Supabase schema introspection and Alembic's autogenerate
agree on the column type.

Why ``ALTER TYPE ... ADD VALUE`` instead of drop-and-recreate
=============================================================

* ``DROP TYPE job_status + CREATE TYPE`` would require dropping and
  re-creating every column that depends on it (``jobs.status``,
  ``job_status_history.from_status``/``.to_status``, the partial
  indexes that pin ``status='in_review'``). A long downtime the
  Render deploy payload can't tolerate.

* PostgreSQL 12+ supports ``ALTER TYPE ... ADD VALUE`` cleanly: it
  adds a new enum variant without rewriting existing rows (the new
  value is appended at the end of the enum sort order, so existing
  rows that reference earlier values are not renumbered or
  re-validated) and the change is committed atomically inside the
  migration's transaction. Pre-PG 12, ``ADD VALUE`` was non-
  transactional and could NOT run inside a migration block — but
  the Render Postgres image is PG 16, so we land squarely in the
  transactional path.

The migration's only side effect is one ``ALTER TYPE ... ADD VALUE``
statement. No backfill (no rows in the new state at upgrade time),
no column default change, no index update. The Postgres
``statement_timeout`` GHA workflow has been observed to occasionally
take ~12s on a Supabase Free-tier instance, so the migration
deliberately does no other DDL.

Python-side
===========

The Python Literal + ``JOB_STATUS_VALUES`` tuple in
:mod:`backend.db.models` get the same addition in the SAME PR — they
share a single source of truth, and ``op.execute()`` doesn't update
those modules, only Postgres. Without the Python updates, the next
Alembic autogenerate would silently think ``job_status_t`` diverged
from Postgres and emit a no-op ``ALTER TYPE`` to "fix" it (no harm,
but noise).

The wire-side guard for ``paused`` rows:

* :func:`routes.applications.create_application_from_job` refuses to
  flip ``paused → applied`` with 409 (same contract as
  ``rejected`` / ``applied``).
* :func:`routes.jobs.get_pending_count`'s WHERE clause is unchanged
  — ``paused`` rows are explicitly excluded from the badge count
  because they're NOT in the auto-apply queue anymore.
* The future :mod:`backend.pipeline.nodes.jobs_boards.runner`
  ``apply_worker`` will SQL-filter for ``status='approved'`` on
  its dequeue query (and may add an explicit guard raise if it
  ever sees a ``paused`` row race in via stale lock — see the
  ``worker.py`` TODO when it lands).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# Revision identifiers, used by Alembic.
revision: str = "0003_add_paused_status"
down_revision: Union[str, None] = "0002_status_history_and_research_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``ALTER TYPE ... ADD VALUE`` (PG 12+) is transactional and
    # idempotent at the SQL level ONLY via ``IF NOT EXISTS`` —
    # without it, re-running this migration on an already-migrated
    # DB fails with ``enum label "paused" already exists``. Postgres
    # 12 added that clause; the Render Postgres image is PG 16 so
    # the safety check is always available.
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'paused'")
    # ``COMMIT`` is implicit in op.execute end-of-block — the
    # migration is single-statement so the Alembic framework wraps
    # it in ``BEGIN ... COMMIT`` itself.


def downgrade() -> None:
    # Postgres 16 supports ``ALTER TYPE ... DROP VALUE`` (added in
    # PG 16 specifically — pre-16 migrations had to recreate the
    # enum, which is why we gated on PG 12+ for the ``ADD VALUE``
    # idempotency above). Use the ``IF EXISTS`` safety guard so a
    # partial-downgrade doesn't poison the next upgrade.
    op.execute("ALTER TYPE job_status DROP VALUE IF EXISTS 'paused'")
