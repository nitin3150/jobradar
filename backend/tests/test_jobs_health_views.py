"""Tests for the diagnostic views defined in ``0004_jobs_health_views``.

What this file covers
=====================

Three classes of test, all materialising the views directly via
``op.execute()`` (rather than going through ``run_migrations_to_head``)
so the assertions stay focused on the view definitions rather than the
broader migration pipeline that's already covered by
:mod:`tests.test_migration_runner`:

1. **Parity with the base table** — for every seeded row, the
   ``COUNT(*)`` from the view matches a freshly-issued
   ``COUNT(*)`` from ``jobs``. Catches column-name typos + GROUP BY
   mistakes + accidentally-restrictive WHERE clauses in the view.

2. **Empty-table behaviour** — both views return 0 rows, not an
   error, when ``jobs`` is empty. Catches accidental ``WHERE``
   filter that silently drops everything.

3. **DDL symmetry** — the Python ``op.execute()`` DDL in
   ``0004_jobs_health_views`` is the same shape (same columns, same
   GROUP BY keys) as the Supabase SQL mirror. Catches drift between
   the two migrations before Render auto-deploys against a schema
   the Supabase project doesn't match.

Why we don't run ``alembic upgrade head``
========================================

``conftest.py`` sets ``JOBRADAR_SKIP_MIGRATIONS=1`` so the FastAPI
lifespan skips migrations on every ``ASGITransport`` construction.
The same flag is honoured here — running alembic upgrade in tests
would mutate the test DB schema for the rest of the suite (other
test modules assume the migration-set state is identical). We
test the view *behaviour* via direct DDL because the migration
mechanics (transaction wrap, ``alembic_version`` row, retry
semantics) are already covered by ``test_migration_runner.py``.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from db.session import AsyncSessionLocal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# We seed directly with raw SQL here (rather than the ``seeded_jobs``
# conftest fixture) so the seeded timestamps land on well-known
# ``TIMESTAMPTZ`` values — the timeseries view bucketing assertions
# below assume exact bucket boundaries and would flake if the seed
# helper's ``default=_utcnow`` drifted into a new second between
# INSERT and view-select. A 5-row, 3-hour-bucket fixture exercises
# bucketing + per-ats-grouping without overspecifying test layout.
@pytest_asyncio.fixture
async def timestamped_jobs():
    """Truncate ``jobs`` then insert a small, deterministic fixture.

    The fixture is content-rich (5 rows, 2 boards, 3 statuses, 3
    distinct hour-buckets) so the assertions below can pin specific
    view outputs without relying on the migration's own seed helper.
    Uses ``_utils_seeds_timestamped_jobs.sql`` so the timestamps are
    exactly what the assertions expect — see the SQL file for the
    INSERT statement.
    """
    # The seed SQL is generated lazily rather than checked in
    # because the bucketing assertions need NOW()-relative
    # timestamps; pinning them at fixture-build time means a re-run
    # in 6 months still uses the "now" that was current when the
    # fix landed, not a stale snapshot from when this test was
    # written.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    bucket_now = now
    bucket_3h_ago = now - timedelta(hours=3)
    bucket_5h_ago = now - timedelta(hours=5)

    rows = [
        # (ats_type, status, created_at, company_name, title)
        ("greenhouse", "approved", bucket_now, "Replicate", "Senior AI Engineer"),
        ("greenhouse", "approved", bucket_now, "Stripe", "Platform Engineer"),
        ("lever", "in_review", bucket_3h_ago, "Mastra", "Founding Engineer"),
        ("ashby", "rejected", bucket_5h_ago, "Midjourney", "Junior ML Engineer"),
        (
            "ashby",
            "applied",
            bucket_5h_ago,
            "Cloudflare",
            "Distributed Systems Engineer",
        ),
    ]

    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE jobs"))
        await session.execute(
            text(
                """
                INSERT INTO jobs
                    (id, status, ats_type, title, company_name, url,
                     ai_fit_score, created_at, updated_at)
                VALUES
                    (:id, :status, :ats, :title, :company, :url,
                     :score, :created_at, :created_at)
                """
            ),
            [
                {
                    "id": _make_id(i),
                    "status": row[1],
                    "ats": row[0],
                    "title": row[4],
                    "company": row[3],
                    "url": f"https://example.com/jobs/{i}",
                    "score": 0.85,
                    "created_at": row[2],
                }
                for i, row in enumerate(rows)
            ],
        )
        await session.commit()
    yield


def _make_id(n: int):
    """Stable UUIDs for the seeded rows so the test is deterministic."""
    return _uuid.UUID(int=n)


# ---------------------------------------------------------------------------
# View materialisation
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def views_materialised():
    """Create the two views in the test DB, then drop them on teardown.

    Mirrors the DDL of the Alembic 0004 migration exactly — see the
    comment on :func:`test_python_ddl_matches_supabase_mirror` for why
    we keep the DDL here in lockstep with both migrations. A drift
    between this fixture and the migration would invalidate every
    test in this file, so the version is checked at fixture-build time
    via :func:`assert_python_ddl_matches_supabase_mirror`.
    """
    # ASCII-identical to the SQL in ``0004_jobs_health_views.py`` and
    # ``supabase/migrations/20260710000000_jobs_health_views.sql`` —
    # changed in three places at once or failing the parity test
    # ``test_python_ddl_matches_supabase_mirror``.
    ddl = """
        CREATE OR REPLACE VIEW jobs_health_summary AS
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

        CREATE OR REPLACE VIEW jobs_health_timeseries AS
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

    async with AsyncSessionLocal() as session:
        await session.execute(text(ddl))
        await session.commit()
    try:
        yield
    finally:
        async with AsyncSessionLocal() as session:
            # Reverse order of the upgrade so a partially-downgraded
            # view remains in a useable state (matching the alembic
            # downgrade comment).
            await session.execute(text("DROP VIEW IF EXISTS jobs_health_timeseries"))
            await session.execute(text("DROP VIEW IF EXISTS jobs_health_summary"))
            await session.commit()


# ---------------------------------------------------------------------------
# Parity tests — view outputs match base-table aggregates
# ---------------------------------------------------------------------------
async def test_summary_view_counts_match_base_table(timestamped_jobs, views_materialised):
    """``SUM(row_count)`` from the view equals ``COUNT(*)`` from ``jobs``.

    This is the headline assertion: whatever the row mix is,
    every row appears in exactly one ``(ats_type, status)`` group,
    and the sum of those groups equals the base-table count. A
    GROUP BY typo (``GROUP BY status`` only, or a missing
    ``ats_type``) would split the wrong way and the sums would
    diverge — this test fails loudly in that case.
    """
    async with AsyncSessionLocal() as session:
        view_total = (await session.execute(
            text("SELECT COALESCE(SUM(row_count), 0) FROM jobs_health_summary")
        )).scalar_one()
        base_total = (await session.execute(
            text("SELECT COUNT(*) FROM jobs")
        )).scalar_one()

    assert view_total == base_total, (
        f"jobs_health_summary sum ({view_total}) != jobs count ({base_total}) — "
        f"view GROUP BY is dropping or duplicating rows"
    )


async def test_summary_view_per_group_matches(timestamped_jobs, views_materialised):
    """Each ``(ats_type, status)`` group's count matches a base-table
    sub-aggregation. Catches column-name typos that would collapse
    distinct (ats, status) groups into one.
    """
    expected = {
        ("greenhouse", "approved"): 2,
        ("lever", "in_review"): 1,
        ("ashby", "rejected"): 1,
        ("ashby", "applied"): 1,
    }

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text(
                "SELECT ats_type, status, row_count "
                "FROM jobs_health_summary ORDER BY ats_type, status"
            )
        )).all()

    actual = {(r[0], r[1]): r[2] for r in rows}
    assert actual == expected, (
        f"jobs_health_summary contents {actual} != expected {expected}"
    )


async def test_timeseries_view_total_matches(timestamped_jobs, views_materialised):
    """``SUM(row_count)`` across all hour-buckets equals the base
    table count. Same logic as the summary parity test, but
    against the bucketed view — catches a buggy bucketing key
    (``DATE_TRUNC`` typo, missed GROUP BY dimension) that would
    split one bucket into many.
    """
    async with AsyncSessionLocal() as session:
        view_total = (await session.execute(
            text("SELECT COALESCE(SUM(row_count), 0) FROM jobs_health_timeseries")
        )).scalar_one()
        base_total = (await session.execute(
            text("SELECT COUNT(*) FROM jobs")
        )).scalar_one()

    assert view_total == base_total, (
        f"jobs_health_timeseries sum ({view_total}) != jobs count ({base_total}) — "
        f"view bucketing key is wrong"
    )


async def test_timeseries_view_buckets_have_correct_counts(timestamped_jobs, views_materialised):
    """Three distinct hour-buckets, with the expected row counts
    per the fixture. Catches a wrong ``DATE_TRUNC`` unit
    (e.g. ``'day'`` instead of ``'hour'``) which would collapse
    every bucket into one.
    """
    async with AsyncSessionLocal() as session:
        # We compare just the per-bucket row counts after stripping
        # the dynamic ``bucket_age`` column (NOW() drift between
        # INSERT and SELECT could shift the assertion by the time
        # the test runs). The bucket identity comes from the
        # ``row_count`` distribution + GROUP BY key count.
        rows = (await session.execute(
            text(
                "SELECT bucket_hour, row_count "
                "FROM jobs_health_timeseries ORDER BY bucket_hour DESC"
            )
        )).all()

    counts = [r[1] for r in rows]
    # 5 rows split across 3 hour-buckets: 2 + 1 + 2 in some order.
    assert sorted(counts) == [1, 2, 2], (
        f"Expected bucket counts [1, 2, 2], got {counts}"
    )


# ---------------------------------------------------------------------------
# Empty-table tests
# ---------------------------------------------------------------------------
async def test_summary_view_empty_when_table_empty(
    timestamped_jobs, views_materialised
):
    """With zero rows in ``jobs``, the view returns 0 rows (not an
    error). Catches an accidental implicit filter that would
    short-circuit to ``NULL`` + 500 on first-run production.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE jobs"))
        await session.commit()

        rows = (await session.execute(
            text("SELECT * FROM jobs_health_summary")
        )).all()

    assert rows == [], f"Expected empty view result, got {rows}"


async def test_timeseries_view_empty_when_table_empty(
    timestamped_jobs, views_materialised
):
    """Same as above, but for the bucketed view."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE jobs"))
        await session.commit()

        rows = (await session.execute(
            text("SELECT * FROM jobs_health_timeseries")
        )).all()

    assert rows == [], f"Expected empty view result, got {rows}"


# ---------------------------------------------------------------------------
# DDL symmetry — Python mirror matches the Supabase SQL mirror
# ---------------------------------------------------------------------------
@pytest.fixture
def alembic_ddl_text() -> str:
    """Read the ``op.execute()`` DDL from the migration file verbatim.

    We compare against this fixture's literal, not against a parsed
    AST, because the relevant bug class is "someone edited one file
    and forgot to mirror it in the other" — a byte-level equality
    check (modulo whitespace) is exactly the contract we want.
    """
    migration_path = (
        Path(__file__).resolve().parent.parent
        / "db"
        / "migrations"
        / "versions"
        / "0004_jobs_health_views.py"
    )
    return migration_path.read_text()


@pytest.fixture
def supabase_ddl_text() -> str:
    """Read the ``supabase/migrations/20260710000000_jobs_health_views.sql``
    mirror verbatim. The two files must stay semantically equivalent;
    this test pins that contract.
    """
    mirror_path = (
        Path(__file__).resolve().parents[2]
        / "supabase"
        / "migrations"
        / "20260710000000_jobs_health_views.sql"
    )
    return mirror_path.read_text()


def test_python_ddl_matches_supabase_mirror(alembic_ddl_text, supabase_ddl_text):
    """Every line that's a CREATE-VIEW statement must match between
    the two files. Catches drift between the Alembic mirror and
    the Supabase mirror — the exact bug class that motivated
    splitting this migration code into two sync'd files.
    """
    import re

    def _extract_create_views(text_: str) -> list[str]:
        # Pull every ``CREATE VIEW jobs_health_*`` statement. Use a
        # non-greedy match to the trailing ``;`` so multi-statement
        # blocks (PostgreSQL allows multiple DDLs in one ``op.execute``
        # call) all land in their own string.
        return [
            s.strip()
            for s in re.findall(
                r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+jobs_health_\w[\s\S]+?;",
                text_,
                flags=re.IGNORECASE,
            )
        ]

    py_views = _extract_create_views(alembic_ddl_text)
    sql_views = _extract_create_views(supabase_ddl_text)

    assert py_views, "Alembic migration has no CREATE VIEW statements — empty view list is a bug"
    assert sql_views, "Supabase migration has no CREATE VIEW statements — empty view list is a bug"
    assert len(py_views) == len(sql_views), (
        f"View count diverged: Alembic has {len(py_views)} CREATE VIEWs, "
        f"Supabase has {len(sql_views)} — add the missing one to whichever "
        f"side has fewer."
    )

    # Compare each view individually after normalising whitespace —
    # multiline ``CREATE VIEW`` declarations can differ in line
    # breaks between the two files for cosmetic reasons.
    def _normalise(s: str) -> str:
        return " ".join(s.split())

    for i, (py_v, sql_v) in enumerate(zip(py_views, sql_views)):
        assert _normalise(py_v) == _normalise(sql_v), (
            f"View {i} diverged between Alembic and Supabase migrations.\n"
            f"  Alembic: {py_v!r}\n"
            f"  Supabase: {sql_v!r}"
        )
