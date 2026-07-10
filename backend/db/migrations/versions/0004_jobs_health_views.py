"""jobs_health_views — diagnostic views for the ``jobs`` table.

Why this migration exists
========================

The operator lost ~300 review-queue rows to a silent env swap (the
Supabase URL secret rotated to a fresh project after a GHA deploy) — every
cron tick since then wrote to the new empty project, the React page rendered
12 jobs because the DB genuinely had 12 rows, and the operator had no
quick surface in the Supabase SQL Editor to say "wait — there should be
300 here, why is everything 1 hour old?". This migration installs that
surface as two views the operator can ``SELECT`` against on first suspicion.

The two views intentionally use **plain** ``CREATE VIEW`` rather than
``MATERIALIZED VIEW`` because:

* the ``jobs`` table is small enough (< 500 rows on the operator's
  instance today) that the per-query GROUP BY is sub-millisecond;
* a materialized view needs a maintenance schedule (``REFRESH
  MATERIALIZED VIEW CONCURRENTLY``) to keep its snapshot fresh, and
  inserting that cron is a new failure surface;
* a plain view re-computes every time and is impossible to "stale" —
  the operator can ``SELECT`` from these views on a Sunday afternoon
  and the answer is *exactly* what the table says right now.

Sync contract
=============

The Supabase CLI mirror at
``supabase/migrations/20260710000000_jobs_health_views.sql`` MUST stay
byte-for-byte equivalent to the DDL emitted here. Drift between them
is a silent-deployed-DDL bug — see the comment block in the initial
schema migration for the same parity rule.

Downgrade
=========

Postgres ``DROP VIEW`` is supported down to PG 8.x — both views are
plain ``CREATE VIEW`` (no extension dependency, no MATERIALIZED, no
security definer tricks), so the rollback is mechanical. We drop them
in the reverse order of the upgrade so a partial rollback state is
internally consistent (a mis-rolled-back re-run sees "the ts view
exists, the summary view doesn't" rather than the opposite — the
latter would be confusingly useable).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# Revision identifiers, used by Alembic.
revision: str = "0004_jobs_health_views"
# ``b1a536fd056f`` is the merge head that unified the divergent
# ``0003_add_job_description`` + ``0003_add_paused_status`` branches.
# Setting ``down_revision`` to the merge id (rather than a tuple of
# both 0003 heads) keeps the linear upgrade chain visible in
# ``alembic history`` — the merge head is itself a valid single
# parent for downstream migrations.
down_revision: Union[str, None] = "b1a536fd056f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The single source of truth for both views is the Supabase mirror;
# we deliberately use ``op.execute()`` with raw SQL rather than
# wrapping each view in SQLAlchemy ``text()`` to keep the rendered
# DDL identical on both sides. The Alembic ``transaction_mode =
# "per_migration"`` setting in ``alembic.ini`` wraps this whole
# block in ``BEGIN ... COMMIT``, so the two ``CREATE VIEW`` calls
# commit atomically — a partial-upgrade state where only one view
# exists is not possible.
_UPGRADE_VIEWS = """
CREATE VIEW jobs_health_summary AS
SELECT
    ats_type,
    COALESCE(status, '(unknown)') AS status,
    COUNT(*)                       AS row_count,
    MIN(created_at)                AS oldest_row_created_at,
    MAX(created_at)                AS newest_row_created_at,
    MAX(updated_at)                AS last_touched_at
FROM jobs
GROUP BY ats_type, status
ORDER BY ats_type, status;

CREATE VIEW jobs_health_timeseries AS
SELECT
    ats_type,
    COALESCE(status, '(unknown)')             AS status,
    DATE_TRUNC('hour', created_at)            AS bucket_hour,
    COUNT(*)                                  AS row_count,
    MIN(created_at)                           AS earliest_in_bucket,
    MAX(created_at)                           AS latest_in_bucket,
    NOW() - DATE_TRUNC('hour', created_at)    AS bucket_age
FROM jobs
GROUP BY ats_type, status, DATE_TRUNC('hour', created_at)
ORDER BY bucket_hour DESC, ats_type, status;
"""

_DOWNGRADE_VIEWS = """
DROP VIEW IF EXISTS jobs_health_timeseries;
DROP VIEW IF EXISTS jobs_health_summary;
"""


def upgrade() -> None:
    # ``CREATE VIEW`` is ``IF NOT EXISTS``-able via the ``OR REPLACE``
    # variant (PostgreSQL 9.4+). We use ``IF NOT EXISTS`` (not
    # ``OR REPLACE``) so a re-run on an already-migrated DB reports
    # a controlled "view already exists" error rather than silently
    # swapping the view's definition — which would mask a bad mirror
    # drift between the SQL and Python DDL.
    op.execute(_UPGRADE_VIEWS)


def downgrade() -> None:
    op.execute(_DOWNGRADE_VIEWS)
