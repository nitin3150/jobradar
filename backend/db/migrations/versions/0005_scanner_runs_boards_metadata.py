"""Boards-scan metadata on ``scanner_runs`` — GHA-cron audit trail.

Why this migration exists
========================

The operator lost ~300 review-queue rows to a silent env swap — every GHA
cron tick since then wrote to a fresh (empty) Supabase project, the React
page rendered 12 jobs because the DB genuinely had 12 rows, and the only
postmortem trail was the GHA run log (per-run_id, ephemeral). For long-term
attribution (``when did this config change first produce zero writes?``),
the audit row needs to live INSIDE the database — closer to the data it
records.

Three new columns
=================

* ``tier``           — the GHA-job that fired the boards-scan invocation:
                        ``active`` (hourly, 1h lookback, BOARDS_LIMIT=200),
                        ``dormant`` (daily 02:20 UTC, 24h lookback, BOARDS_LIMIT=0),
                        ``cleanup`` (weekly Sunday 04:20 UTC, dry-run missing-orgs).
                        Free-text rather than ENUM so a future tier (``burst``,
                        ``manual``) lands without an ALTER TYPE migration. NULL
                        for funding / remote / ngos / oss scanners that don't
                        tier themselves.
* ``env_hash``       — sha256-hex digest of the env vars + GITHUB_SHA that
                        governed the run. Lets a postmortem query answer
                        "which cron tick first changed HTTP_TIMEOUT from 30 to
                        10?" in one ``GROUP BY env_hash`` over the last 30
                        days rather than scraping 720 GHA run logs.
* ``jobs_persisted`` — count of rows the boards-scan *successfully wrote*
                        to the ``jobs`` table on this run. ``items_found``
                        (existing column) is the count of jobs the RUNNER
                        returned for scoring; ``jobs_persisted`` is the
                        SCORING → Supabase stage that landed. The two-column
                        split lets a future alerting rule fire on
                        ``items_found > jobs_persisted`` (scored-but-not-
                        persisted = scoring-service error or Supabase rate-
                        limit window).

New index
=========

``(tier, started_at DESC)`` — supports the per-tier history query
(``SELECT ... WHERE tier = 'active' ORDER BY started_at DESC LIMIT 50``)
without a sort; tier+started_at is exactly the operator's first-question
query in a postmortem.

Not a backfill
==============

Pre-migration runs left these columns NULL — intentionally. There is no
meaningful way to derive a ``tier`` or ``env_hash`` from a run that
happened before this migration; cron's on-disk state is the only source
of truth for those runs and re-deriving would be a different shape than
what fresh runs emit (different column subset, different env-var names).
A future maintainer should treat pre-migration rows as "tier=unknown"
and ``env_hash=NULL`` rows; do not run a backfill.

Sync contract
=============

Mirrored to ``supabase/migrations/20260710010000_scanner_runs_boards_metadata.sql``
— both files travel together or the Supabase project drifts from the
Render Alembic-managed DB. The mirroring rule (set in
``20260101000000_initial_schema.sql``'s comment block) holds.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# Revision identifiers, used by Alembic.
revision: str = "0005_scanner_runs_boards_metadata"
down_revision: Union[str, None] = "0004_jobs_health_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_with_check_default() -> str:
    """``Text NULL`` / ``Integer NULL`` columns are pure DDL — no
    backfill or constraint expression is needed. The function is a
    docstring placeholder rather than a real abstraction; calling
    ``op.add_column(...)`` three times in a row is the cleanest
    representation. Kept as a function-shaped comment so the
    downgrade (which mirrors with ``op.drop_column`` in the same
    order) is visually obvious when read top-to-bottom.
    """


def upgrade() -> None:
    # Three additive NULL-able columns + one index. NULL-able (not
    # server_defaulted) so funding / remote / ngos / oss scanners
    # can leave them NULL — the boards-scan writer fills them; the
    # other scanners stay passive consumers of the same table.
    op.add_column(
        "scanner_runs",
        sa.Column("tier", sa.Text(), nullable=True),
    )
    op.add_column(
        "scanner_runs",
        sa.Column("env_hash", sa.Text(), nullable=True),
    )
    # ``INTEGER`` matches the existing ``items_found`` column's type
    # so a future ``COALESCE(items_found, jobs_persisted, 0)``
    # aggregate in operator queries doesn't trip a type-coercion.
    op.add_column(
        "scanner_runs",
        sa.Column("jobs_persisted", sa.Integer(), nullable=True),
    )
    op.create_index(
        "idx_scanner_runs_tier_started",
        "scanner_runs",
        ["tier", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    # Reverse order of upgrade — index first (its columns are about
    # to drop), then columns in reverse order, so any concurrently
    # running query that depends on the column sees the index gone
    # BEFORE the column is dropped. That ordering is a no-op for a
    # single-process migration under ``transaction_mode = "per_migration"``
    # but it stays correct under ``autocommit`` mode.
    op.drop_index("idx_scanner_runs_tier_started", table_name="scanner_runs")
    op.drop_column("scanner_runs", "jobs_persisted")
    op.drop_column("scanner_runs", "env_hash")
    op.drop_column("scanner_runs", "tier")
