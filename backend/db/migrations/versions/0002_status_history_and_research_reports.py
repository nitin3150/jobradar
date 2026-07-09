"""status history + research reports + jobs.posted_at — JobRadar v1 evolution.

Three additive changes in a single migration (we follow the v1 convention
of one migration per logical delivery; future migrations will be smaller
diffs chained off this one).

Why a new ``job_status_history`` table instead of new columns
=============================================================

The operator ('i need all the data for each status') asked for the
ability to time-travel the lifecycle of a single job: when did it enter
``in_review``, when did it get approved, when did it get applied to,
when did the recruiter email back. Adding per-status columns to the
``jobs`` table (e.g. ``approved_at``, ``applied_at``) only stores the
*latest* timestamp per status — the moment a job goes ``approved →
rejected → approved`` again, the previous ``approved_at`` is lost.

A history table records every transition with a FROM/TO pair and a
``changed_at`` timestamp so an analyst can answer those questions from
SQL alone::

    -- "how many is_review → approved transitions happened last week?"
    SELECT COUNT(*) FROM job_status_history
    WHERE from_status = 'in_review' AND to_status = 'approved'
      AND changed_at >= now() - interval '7 days';

    -- "average dwell time in 'in_review' before approval?"
    SELECT AVG(EXTRACT(EPOCH FROM (h1.changed_at - h2.changed_at)))
    FROM job_status_history h1
    JOIN job_status_history h2
      ON h1.job_id = h2.job_id
     AND h2.to_status = 'in_review'
    WHERE h1.from_status = 'in_review' AND h1.to_status = 'approved';

The history row is written in the same transaction as the ``jobs``
``status`` UPDATE so the two writes either both land or both roll back
— a future operator query can never observe a status with no history.

Why ``research_reports`` instead of a column on ``jobs``
=========================================================

A single job can have multiple research reports over time (after
re-applying, when the company changes its blog, etc.) — a 1-to-N
relation. The sync ``POST /api/jobs/{id}/research`` endpoint reads the
latest ready report first; the ``requested_at DESC`` index keeps that
lookup O(1).

The ``websearch_payload`` column is reserved (always NULL in v1) so a
future Serper/Apify integration can plumb results in without a migration.
``model_used`` records which LLM produced the report for cost-tagging.

Why ``posted_at`` / ``source_updated_at`` on ``jobs``
=====================================================

Some boards (Greenhouse, Lever) expose ``updated_at`` on each posting
— those values are scraped once and frozen on the ``jobs`` row so an
analyst can ask "what time did the posting actually appear, vs. when
did we scrape it?". Nullable because Ashby doesn't expose either
field consistently. The existing ``created_at`` / ``updated_at`` columns
were repurposed to "when we first saw the row" / "when our DB last
touched it" respectively.

The ``idx_jobs_posted_at`` partial index keeps the "newest postings in
the last N days" filter sub-millisecond even when ``jobs`` accumlates
tens of thousands of historical rows. Same shape as
:data:`idx_jobs_in_review_deadline` (the partial-where-status filter)
so the planner can satisfy both filters from a single index.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "0002_status_history_and_research_reports"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----------------------------------------------------------------------
    # 1. job_status_history — one row per status transition.
    # ----------------------------------------------------------------------
    op.create_table(
        "job_status_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ``from_status`` is NULL for the initial 'seeded from board scrape'
        # row so analysts can distinguish "first row for this job" from
        # subsequent transitions without an extra flag column. Free text
        # (not a Postgres ENUM) so a future ``JobStatus`` expansion
        # doesn't require an ``ALTER TYPE`` — the Pydantic Literal in
        # routes.jobs remains the wire-side gatekeeper.
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # ``source`` describes *who* drove the transition. Loose-text so a
        # future caller can add new sources (e.g. "auto_apply_worker")
        # without an ALTER TABLE. The v1 set is:
        #   "scorer"     — initial in_review insert via scoring_service
        #   "user"       — manual approve/reject/mark-applied from the UI
        #   "auto_apply" — future Playwright worker handoff
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'scorer'"),
        ),
        # Free-form operator note captured at the transition time. Bounded
        # to 2 KB by an explicit length guard on the PATCH route so a
        # runaway note cannot balloon row sizes.
        sa.Column("note", sa.Text(), nullable=True),
    )
    # (job_id, changed_at DESC) — primary read path is "show me the
    # full history for job X, newest first".
    op.create_index(
        "idx_job_status_history_job_changed",
        "job_status_history",
        ["job_id", sa.text("changed_at DESC")],
    )
    # (to_status, changed_at DESC) — analytics path "how many rows
    # hit status X in the last N days?" plus "average dwell".
    op.create_index(
        "idx_job_status_history_to_changed",
        "job_status_history",
        ["to_status", sa.text("changed_at DESC")],
    )

    # ----------------------------------------------------------------------
    # 2. research_reports — Interview Prep output + future web-search plumb.
    # ----------------------------------------------------------------------
    op.create_table(
        "research_reports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        # ``ON DELETE SET NULL`` + ``nullable=True`` because a research
        # report is an independent, expensive LLM artefact — if its
        # parent Job is purged, the report should still be queryable for
        # retrospective analysis (mirror of how :class:`db.models.Application`
        # treats its ``job_id`` FK).
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Lifecycle: 'pending' → 'ready' | 'failed'. TEXT (not ENUM) so
        # a future 'expired' state (e.g. triggered by a re-scrape staleness
        # check) can land without a migration. The API surface in v1 only
        # writes 'ready'/'failed' — 'pending' is reserved for the async
        # polling UX described in the design spec; the sync endpoint
        # completes before insert so callers never observe 'pending' in
        # v1.
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ready'"),
        ),
        # Markdown body. Free text, no length cap at the DB layer; the
        # endpoint enforces a 30 KB ceiling so a runaway model output
        # cannot crash the React renderer.
        sa.Column("content", sa.Text(), nullable=True),
        # ``model_used`` records which LLM produced the report so cost
        # allocation can slice by model and so a future A/B between Groq
        # and NVIDIA can be analysed without scraping logs.
        sa.Column("model_used", sa.Text(), nullable=True),
        # Reserved for the 'LLM + websearch' future. JSONB so future
        # Serper results can be stored as a structured payload (rank,
        # url, snippet, timestamp) without another migration. Always
        # NULL in v1.
        sa.Column(
            "websearch_payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        # Error message when status='failed'. NULL on success.
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_research_reports_job_requested",
        "research_reports",
        ["job_id", sa.text("requested_at DESC")],
    )

    # ----------------------------------------------------------------------
    # 3. jobs.posted_at + source_updated_at — board-published timestamps.
    # ----------------------------------------------------------------------
    op.add_column(
        "jobs",
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "source_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial index keeps the "newest postings in the last N days" filter
    # sub-millisecond. Same `WHERE posted_at IS NOT NULL` precondition as
    # the in-review-deadline index — a NULL posted_at means the board
    # didn't tell us a timestamp, so the index skips those rows.
    op.create_index(
        "idx_jobs_posted_at",
        "jobs",
        [sa.text("posted_at DESC")],
        postgresql_where=sa.text("posted_at IS NOT NULL"),
    )


def downgrade() -> None:
    # Strict reverse-dependency order: indexes before columns, then tables.
    op.drop_index("idx_jobs_posted_at", table_name="jobs")
    op.drop_column("jobs", "source_updated_at")
    op.drop_column("jobs", "posted_at")
    op.drop_index("idx_research_reports_job_requested", table_name="research_reports")
    op.drop_table("research_reports")
    op.drop_index(
        "idx_job_status_history_to_changed", table_name="job_status_history"
    )
    op.drop_index(
        "idx_job_status_history_job_changed", table_name="job_status_history"
    )
    op.drop_table("job_status_history")
