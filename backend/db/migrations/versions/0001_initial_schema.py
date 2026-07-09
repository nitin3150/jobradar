"""initial schema — JobRadar persistence layer (v1).

This is the only migration needed for v1 — it creates the entire
:mod:`backend.db.models` declarative surface from scratch on a fresh
Supabase / Postgres 16 database.

Why one migration and not ten
=============================
The user spec ("schema + migration files only" + "production-proof")
explicitly trades off a longer, single migration against a chain of
fractured ones. The trade-off:

* **Single migration** — what we picked. Atomic: a failure either
  applies everything or rolls everything back. Reviewable as a single
  diff. Easy to mirror in Supabase's SQL editor for ops.
* **Per-domain migrations** — what we did *not* pick. Better at
  mimicking a long-running production rollout, but adds ceremony and
  makes every fresh database install apply N diffs in sequence.

Future migrations (additive only) will be much smaller and piggy-back
on ``down_revision = "0001_initial_schema"``.

Section order in ``upgrade()``
==============================

1. ``pgcrypto`` extension — required for ``gen_random_uuid()``.
2. Postgres enum types — must exist BEFORE any column that references
   them via ``CREATE TABLE``.
3. Every table in dependency order: leaves first, FK roots last. The
   ``companies`` table is a root (5 of 16 tables FK to it), so it lands
   early. The singletons (``preferences``, ``pipeline_status``) land
   last so they don't hold up the rest of the install.

The ``downgrade()`` runs in strict reverse order to keep
foreign-key-cascade satisfied.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Helpers — kept local so the migration is fully self-contained.
# ---------------------------------------------------------------------------
def _create_enum(name: str, values: Sequence[str]) -> None:
    """``CREATE TYPE <name> AS ENUM (...)`` — ``checkfirst=True`` so re-runs
    against an already-migrated DB are idempotent.
    """
    enum = postgresql.ENUM(*values, name=name)
    enum.create(op.get_bind(), checkfirst=True)


def _drop_enum(name: str) -> None:
    """``DROP TYPE IF EXISTS <name>`` — best-effort; ignores the type-not-found
    case so a partial apply doesn't poison the rollback. ``CASCADE`` is
    deliberately omitted because the explicit ``op.drop_table`` calls above
    already remove the legitimate dependents; using ``CASCADE`` would
    silently drop any unrelated table that an operator later attached the
    type to.
    """
    op.execute(f"DROP TYPE IF EXISTS {name}")


# ---------------------------------------------------------------------------
def upgrade() -> None:
    bind = op.get_bind()

    # -----------------------------------------------------------------------
    # 1. pgcrypto — provides ``gen_random_uuid()``. Supabase enables it by
    #    default; ``CREATE EXTENSION IF NOT EXISTS`` is a no-op there but
    #    saves the operator from a confusing error on a vanilla PG 16 install.
    # -----------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -----------------------------------------------------------------------
    # 2. Enum types. Keep this list in sync with the ``postgresql.ENUM``
    #    instances in :mod:`backend.db.models`.
    # -----------------------------------------------------------------------
    _create_enum(
        "company_category",
        ("boards", "funding", "ngos", "oss", "remote"),
    )
    _create_enum(
        "company_status",
        ("saved", "interested", "dismissed", "outreach_sent", "engaged"),
    )
    _create_enum(
        "job_status",
        ("in_review", "approved", "rejected", "applied", "flagged"),
    )
    _create_enum(
        "application_status",
        ("submitted", "interview", "rejected", "offer", "ghosted"),
    )
    _create_enum("qa_answer_type", ("short_text", "long_text"))
    _create_enum("outreach_type", ("email", "twitter_dm", "linkedin"))
    _create_enum(
        "scanner_kind",
        ("funding", "remote", "ngos", "oss", "boards"),
    )
    _create_enum(
        "ats_board",
        ("ashby", "lever", "greenhouse", "remotive", "remoteok", "hackernews"),
    )
    _create_enum("ats_org_status", ("active", "missing", "benched"))
    _create_enum("pipeline_state", ("idle", "running", "error"))

    # -----------------------------------------------------------------------
    # 3. Tables — in FK dependency order: ``companies`` first because 5 of 16
    #    tables FK to its ``id``; the singletons last because nothing FKs
    #    to them and they trivially satisfy the dependency requirements.
    # -----------------------------------------------------------------------
    companies_table = op.create_table(
        "companies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("organization", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column(
            "category",
            postgresql.ENUM(
                "boards", "funding", "ngos", "oss", "remote",
                name="company_category",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "score",
            sa.Float(),
            sa.CheckConstraint(
                "score >= 0.0 AND score <= 1.0", name="companies_score_range"
            ),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("primary_language", sa.Text(), nullable=True),
        sa.Column("difficulty", sa.Text(), nullable=True),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.Column("company_summary", sa.Text(), nullable=True),
        sa.Column(
            "hiring_signals",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "saved", "interested", "dismissed", "outreach_sent", "engaged",
                name="company_status",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'saved'"),
        ),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_companies_feed",
        "companies",
        ["category", "status", sa.text("published_at DESC")],
    )
    op.create_index("idx_companies_external_id", "companies", ["external_id"])

    # --- 5 raw_scrapes_* landing tables ----------------------------------------
    # Each matches the Company grain in columns NOT expressly on the JSONB
    # payload, so a downgrade can introspect the scrape without parsing raw
    # rows. The FK to companies.id is left ``ON DELETE SET NULL`` so a
    # company demotion never cascades the raw crawl data away.
    #
    # Index names follow the ``idx_raw_<short>_...`` convention declared by
    # :mod:`db.models` so a future ``alembic revision --autogenerate`` does
    # not see them as renamed.
    for table_name in (
        "raw_scrapes_funding",
        "raw_scrapes_remote",
        "raw_scrapes_ngos",
        "raw_scrapes_oss",
        "raw_scrapes_boards",
    ):
        op.create_table(
            table_name,
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                server_default=sa.text("gen_random_uuid()"),
                primary_key=True,
            ),
            sa.Column("external_id", sa.Text(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column(
                "company_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("companies.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "scraped_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "promoted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.create_index(
            f"idx_raw_{table_name.removeprefix('raw_scrapes_')}_scraped_at",
            table_name,
            [sa.text("scraped_at DESC")],
        )

    # `raw_scrapes_funding` and `raw_scrapes_boards` are the only landing
    # tables most heavily deduped today; give them explicit external_id
    # indexes so the boards runner's "already promoted?" lookup is O(log n).
    op.create_index(
        "idx_raw_funding_external",
        "raw_scrapes_funding",
        ["external_id"],
    )
    op.create_index(
        "idx_raw_boards_external",
        "raw_scrapes_boards",
        ["external_id"],
    )

    # --- scanner_runs --------------------------------------------------------
    op.create_table(
        "scanner_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "scanner",
            postgresql.ENUM(
                "funding", "remote", "ngos", "oss", "boards",
                name="scanner_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state",
            postgresql.ENUM(
                "idle", "running", "error",
                name="pipeline_state",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "items_found",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "error_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_scanner_runs_scanner_started",
        "scanner_runs",
        ["scanner", sa.text("started_at DESC")],
    )

    # --- jobs ---------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "in_review", "approved", "rejected", "applied", "flagged",
                name="job_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("ats_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "ai_fit_score",
            sa.Float(),
            sa.CheckConstraint(
                "ai_fit_score IS NULL OR (ai_fit_score >= 0.0 AND ai_fit_score <= 1.0)",
                name="jobs_ai_fit_score_range",
            ),
            nullable=True,
        ),
        sa.Column("ai_fit_reasoning", sa.Text(), nullable=True),
        sa.Column(
            "review_deadline",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Partial index — keep the in-review queue sub-millisecond even when
    # ``jobs`` grows into the hundreds-of-thousands range.
    op.create_index(
        "idx_jobs_in_review_deadline",
        "jobs",
        [sa.text("review_deadline ASC")],
        postgresql_where=sa.text("status = 'in_review'"),
    )
    op.create_index(
        "idx_jobs_status_created",
        "jobs",
        ["status", sa.text("created_at DESC")],
    )
    op.create_index("idx_jobs_external", "jobs", ["external_id"])

    # --- applications -------------------------------------------------------
    op.create_table(
        "applications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_title", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "submitted", "interview", "rejected", "offer", "ghosted",
                name="application_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "last_email_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "submission_screenshot_path",
            sa.Text(),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_applications_status_submitted",
        "applications",
        ["status", sa.text("submitted_at DESC")],
    )

    # --- qa_bank_entries ----------------------------------------------------
    op.create_table(
        "qa_bank_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("question_pattern", sa.Text(), nullable=False, unique=True),
        sa.Column("canonical_question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column(
            "answer_type",
            postgresql.ENUM(
                "short_text", "long_text",
                name="qa_answer_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "times_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_qa_bank_times_used",
        "qa_bank_entries",
        [sa.text("times_used DESC")],
    )

    # --- resumes ------------------------------------------------------------
    op.create_table(
        "resumes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("storage_path", sa.Text(), nullable=False),
    )
    # Single-default invariant: at most one row may have
    # ``is_default = true``. The Postgres partial unique index enforces
    # this in the engine — the application layer no longer needs to
    # demote siblings on each PATCH.
    op.create_index(
        "uq_resumes_single_default",
        "resumes",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )

    # --- outreach_messages --------------------------------------------------
    op.create_table(
        "outreach_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "type",
            postgresql.ENUM(
                "email", "twitter_dm", "linkedin",
                name="outreach_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("resume_id", sa.Text(), nullable=True),
        sa.Column("resume_name", sa.Text(), nullable=True),
        sa.Column("qa_snippet_id", sa.Text(), nullable=True),
        sa.Column("qa_snippet", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_outreach_company_created",
        "outreach_messages",
        ["company_id", sa.text("created_at DESC")],
    )

    # --- preferences (singleton) -------------------------------------------
    op.create_table(
        "preferences",
        sa.Column(
            "id",
            sa.Integer(),
            sa.CheckConstraint("id = 1", name="preferences_singleton"),
            primary_key=True,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "target_roles",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "review_window_hours",
            sa.Float(),
            sa.CheckConstraint(
                "review_window_hours >= 0.5 AND review_window_hours <= 48.0",
                name="preferences_window_range",
            ),
            nullable=False,
            server_default=sa.text("2.0"),
        ),
        sa.Column(
            "job_fit_threshold",
            sa.Float(),
            sa.CheckConstraint(
                "job_fit_threshold >= 0.0 AND job_fit_threshold <= 1.0",
                name="preferences_threshold_range",
            ),
            nullable=False,
            server_default=sa.text("0.6"),
        ),
        sa.Column(
            "send_followup_emails",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        # Optional seniority band — drives
        # :func:`utils.filters.is_relevant_role`. Free-text columns
        # rather than Postgres ENUM so adding a new tier in
        # :data:`utils.filters.SENIORITY_TIERS` doesn't require an
        # ``ALTER TYPE`` migration; the Pydantic ``SeniorityTier``
        # Literal guards the wire form so only known values reach the
        # DB. ``nullable=True`` so the existing default preference
        # row inserts cleanly via the singleton's ``id = 1`` PK.
        sa.Column("min_seniority", sa.Text(), nullable=True),
        sa.Column("max_seniority", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- pipeline_status (singleton) ---------------------------------------
    op.create_table(
        "pipeline_status",
        sa.Column(
            "id",
            sa.Integer(),
            sa.CheckConstraint("id = 1", name="pipeline_status_singleton"),
            primary_key=True,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "state",
            postgresql.ENUM(
                "idle", "running", "error",
                name="pipeline_state",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'idle'"),
        ),
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_run_duration_seconds",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "last_run_counts",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "recent_error",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "interval_hours",
            sa.Integer(),
            sa.CheckConstraint(
                "interval_hours IN (1, 2, 4, 6, 12, 24)",
                name="pipeline_status_interval_allowed",
            ),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "schedule_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # --- board_seen_jobs ----------------------------------------------------
    op.create_table(
        "board_seen_jobs",
        sa.Column("job_id_hash", sa.Text(), primary_key=True),
        sa.Column(
            "board",
            postgresql.ENUM(
                "ashby", "lever", "greenhouse", "remotive",
                "remoteok", "hackernews",
                name="ats_board",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "times_seen",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_index(
        "idx_board_seen_board_last_seen",
        "board_seen_jobs",
        ["board", sa.text("last_seen_at DESC")],
    )

    # --- ats_discovered_orgs -----------------------------------------------
    op.create_table(
        "ats_discovered_orgs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "board",
            postgresql.ENUM(
                "ashby", "lever", "greenhouse", "remotive",
                "remoteok", "hackernews",
                name="ats_board",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "missing", "benched",
                name="ats_org_status",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("board", "slug", name="uq_ats_orgs_board_slug"),
    )
    op.create_index(
        "idx_ats_orgs_status",
        "ats_discovered_orgs",
        ["status", "consecutive_failures"],
    )


# ---------------------------------------------------------------------------
def downgrade() -> None:
    # Reverse dependency order — children first, parents last. The
    # ``CASCADE`` on type drop catches orphaned array / table references
    # if someone has manually added FKs to one of our types.
    op.drop_table("ats_discovered_orgs")
    op.drop_table("board_seen_jobs")
    op.drop_table("pipeline_status")
    op.drop_table("preferences")
    # Note: the seniority columns added in this schema revision are
    # free-text so they have no associated Postgres ENUM to drop.
    op.drop_table("outreach_messages")
    op.drop_table("resumes")
    op.drop_table("qa_bank_entries")
    op.drop_table("applications")
    op.drop_table("jobs")
    op.drop_table("scanner_runs")
    for table_name in (
        "raw_scrapes_boards",
        "raw_scrapes_oss",
        "raw_scrapes_ngos",
        "raw_scrapes_remote",
        "raw_scrapes_funding",
    ):
        op.drop_table(table_name)
    op.drop_table("companies")

    # Then drop enum types.
    for name in (
        "pipeline_state",
        "ats_org_status",
        "ats_board",
        "scanner_kind",
        "outreach_type",
        "qa_answer_type",
        "application_status",
        "job_status",
        "company_status",
        "company_category",
    ):
        _drop_enum(name)

    # The pgcrypto extension is intentionally NOT dropped — other
    # systems may use it; leaving it installed is harmless.
