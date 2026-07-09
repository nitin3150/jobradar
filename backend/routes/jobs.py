"""Jobs router — review queue backed by the real Supabase ``jobs`` table.

The wire shape (and React consumer expectations) is unchanged from
the previous in-memory version; :class:`Job` etc. still match what
``frontend/src/api/jobs.js`` consumes:

* ``GET /api/jobs?status=in_review&page_size=50`` →
  ``{"jobs": [...], "total": int}``.
* ``GET /api/jobs/pending-count`` → ``{"count": int}`` — number of
  jobs in ``status == "in_review"``.
* ``POST /api/jobs/{job_id}/approve`` — flips ``status`` to
  ``"approved"`` and clears ``review_deadline``.
* ``POST /api/jobs/{job_id}/reject`` — flips ``status`` to
  ``"rejected"`` and clears ``review_deadline``.

Read paths source from :class:`db.models.Job` via the async
SQLAlchemy session factory. Writes from :mod:`services.scoring_service`
land in the same row via an ``INSERT … ON CONFLICT (id) DO UPDATE``
upsert keyed on a deterministic :func:`uuid5`-generated id.

Route-ordering note: ``GET /jobs/pending-count`` is declared BEFORE
the ``{job_id}`` action routes so a future ``GET /jobs/{job_id}``
addition does not shadow the literal pending-count path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, get_args
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import models as db_models
from db.session import get_session, require_database_configured


router = APIRouter()


# At the very first request, surface a clear mis-config error instead
# of a confusing asyncpg stack trace. Cheap to call — module-level.
require_database_configured()


# ----------------------------------------------------------------------
# Enums + Pydantic wire shape
# ----------------------------------------------------------------------
JobStatus = Literal["in_review", "approved", "rejected", "applied", "flagged"]

# Single source of truth: read the Literal's string members at import
# time so a future ``JobStatus`` expansion automatically widens this
# guard set without a second hand-edited list to keep in sync.
# Used in :func:`list_jobs` as an allow-list so an unknown
# ``?status=<anything>`` query short-circuits to an empty result
# instead of reaching the SQLAlchemy ``jobs.status = $1::job_status``
# comparison, where ``<anything>`` is not a valid enum value and
# Postgres raises ``invalid input value for enum job_status: ...``.
JOB_STATUS_VALUES: frozenset[str] = frozenset(get_args(JobStatus))


class Job(BaseModel):
    id: str
    status: JobStatus
    ats_type: str
    title: str
    company_name: str
    url: str
    ai_fit_score: float | None = Field(default=None, ge=0.0, le=1.0)
    ai_fit_reasoning: str | None = None
    review_deadline: str | None = None


class JobListResponse(BaseModel):
    jobs: list[Job]
    total: int


class PendingCountResponse(BaseModel):
    count: int


# ----------------------------------------------------------------------
# Translation helpers — DB row → Pydantic wire shape
# ----------------------------------------------------------------------
def _job_row_to_pydantic(row: db_models.Job) -> Job:
    """Map an ORM ``Job`` row to the wire-shape Pydantic ``Job``.

    Two non-trivial conversions: ``id`` is a real Postgres UUID but
    the contract is a string, and ``review_deadline`` is a real
    ``datetime`` but the contract is an ISO 8601 string with the
    trailing ``Z`` so tests / frontend code can match the rest of
    JobRadar's wire format (``datetime.now(timezone.utc).isoformat()``
    with the ``+00:00`` ⟶ ``Z`` rewrite).
    """
    deadline_iso: str | None = None
    if row.review_deadline is not None:
        deadline_iso = row.review_deadline.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    return Job(
        id=str(row.id),
        status=row.status,
        ats_type=row.ats_type,
        title=row.title,
        company_name=row.company_name,
        url=row.url,
        ai_fit_score=row.ai_fit_score,
        ai_fit_reasoning=row.ai_fit_reasoning,
        review_deadline=deadline_iso,
    )


# ----------------------------------------------------------------------
# Seeding helper — kept as a test seam so tests can install a known
# fixture in place of the previous in-memory dict. Production routes
# never call this. Imports cleanly because ``_seed_job_rows`` is the
# only DB-touching helper used outside of the route handlers.
# ----------------------------------------------------------------------
_TEST_SEED_RECORDS_RAW: list[dict] = [
    {
        "id_uuid_text": "j_1",
        "seed_marker": True,
        "status": "in_review",
        "ats_type": "ashby",
        "title": "Senior AI Engineer",
        "company_name": "Replicate",
        "url": "https://replicate.com/careers",
        "ai_fit_score": 0.86,
        "ai_fit_reasoning": "Strong match — LLM inference + Python + open-source fluency.",
        "review_deadline_isostring": "in_2_hours",
    },
    {
        "id_uuid_text": "j_2",
        "seed_marker": True,
        "status": "in_review",
        "ats_type": "lever",
        "title": "Founding Engineer",
        "company_name": "Mastra",
        "url": "https://mastra.ai/careers",
        "ai_fit_score": 0.78,
        "ai_fit_reasoning": "TypeScript + AI agent infrastructure; matches your skill stack.",
        "review_deadline_isostring": "in_5_hours",
    },
    {
        "id_uuid_text": "j_3",
        "seed_marker": True,
        "status": "approved",
        "ats_type": "greenhouse",
        "title": "Backend Engineer",
        "company_name": "Vercel",
        "url": "https://vercel.com/careers",
        "ai_fit_score": 0.91,
        "ai_fit_reasoning": "High-priority match — Node + edge runtime experience applies directly.",
        "review_deadline_isostring": None,
    },
    {
        "id_uuid_text": "j_4",
        "seed_marker": True,
        "status": "rejected",
        "ats_type": "ashby",
        "title": "Junior ML Engineer",
        "company_name": "Midjourney",
        "url": "https://midjourney.com/careers",
        "ai_fit_score": 0.42,
        "ai_fit_reasoning": "Below your preferred threshold; aligned with your directional interests but lacks senior scope.",
        "review_deadline_isostring": None,
    },
    {
        "id_uuid_text": "j_5",
        "seed_marker": True,
        "status": "applied",
        "ats_type": "greenhouse",
        "title": "Distributed Systems Engineer",
        "company_name": "Cloudflare",
        "url": "https://cloudflare.com/careers",
        "ai_fit_score": 0.74,
        "ai_fit_reasoning": "Applied via apply_worker — Rust + edge experience.",
        "review_deadline_isostring": None,
    },
    {
        "id_uuid_text": "j_6",
        "seed_marker": True,
        "status": "flagged",
        "ats_type": "remotive",
        "title": "Remote Solutions Architect",
        "company_name": "Doist",
        "url": "https://doist.com/careers",
        "ai_fit_score": 0.58,
        "ai_fit_reasoning": "Flagged for manual review — overlaps your stack but the role is IC-track not engineer-track.",
        "review_deadline_isostring": None,
    },
]


_SEED_NAMESPACE = UUID("12345678-1234-5678-1234-567812345678")


def _seed_id_for(marker: str) -> UUID:
    """Deterministic UUID for a seed marker (``"j_1"`` ⟶ stable UUID).

    Uses :func:`uuid.uuid5` with a fixed namespace so re-running the
    seed fixture against a clean table produces the *same* primary
    keys, which means test code can hard-code ``str(uuid_for('j_1'))``
    in path URLs and pickets the right row across runs.
    """
    return uuid5(_SEED_NAMESPACE, marker)


async def _seed_job_rows(session: AsyncSession) -> None:
    """Truncate ``jobs`` then insert the canonical seed fixture.

    This replaces the previous ``_jobs._seed()`` in-memory helper. The
    ``external_id`` column is set to the marker ``"seed:<j_n>"`` so
    tests can distinguish fixture rows from scoring-service-produced
    winners (which leave ``external_id`` NULL).

    Tests call this in ``setUp`` — production never does.
    """
    from sqlalchemy import delete as sa_delete

    _now = datetime.now(timezone.utc)

    # Wipe any existing rows first so a rerun of the seed fixture
    # doesn't accumulate duplicates. ``DELETE`` here is fine for the
    # test schema — production never invokes this path.
    await session.execute(sa_delete(db_models.Job))
    await session.flush()

    for raw in _TEST_SEED_RECORDS_RAW:
        if raw["review_deadline_isostring"] == "in_2_hours":
            deadline = _now + timedelta(hours=2)
        elif raw["review_deadline_isostring"] == "in_5_hours":
            deadline = _now + timedelta(hours=5)
        else:
            deadline = None
        row = db_models.Job(
            id=_seed_id_for(raw["id_uuid_text"]),
            company_name=raw["company_name"],
            status=raw["status"],
            ats_type=raw["ats_type"],
            title=raw["title"],
            url=raw["url"],
            ai_fit_score=raw["ai_fit_score"],
            ai_fit_reasoning=raw["ai_fit_reasoning"],
            review_deadline=deadline,
            external_id=f"seed:{raw['id_uuid_text']}",
        )
        session.add(row)
    await session.commit()


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@router.get("/pending-count", response_model=PendingCountResponse)
async def get_pending_count(
    session: AsyncSession = Depends(get_session),
) -> PendingCountResponse:
    """Number of jobs in the ``in_review`` queue — drives the Navbar badge."""
    count = await session.scalar(
        select(func.count(db_models.Job.id)).where(db_models.Job.status == "in_review")
    )
    return PendingCountResponse(count=int(count or 0))


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> JobListResponse:
    """List jobs with optional ``status`` filter.

    Newest in-review rows by review deadline DESC NULLS LAST, then any
    terminal-status rows below. ``total`` reflects the matched set
    *before* slicing so the React list can render "showing N of M".
    """
    stmt = select(db_models.Job)
    count_stmt = select(func.count(db_models.Job.id))
    if status_filter:
        # Unknown values short-circuit before the SQLAlchemy ORM
        # builds a ``jobs.status = $1::job_status`` bindparam cast;
        # letting an arbitrary client string reach PG would raise
        # ``invalid input value for enum job_status: '...'`` on the
        # live types. Returning an empty envelope (200 + []) matches
        # the wire shape the React ``usePendingCount`` /
        # ``useJobs`` hooks already render.
        if status_filter not in JOB_STATUS_VALUES:
            return JobListResponse(jobs=[], total=0)
        stmt = stmt.where(db_models.Job.status == status_filter)
        count_stmt = count_stmt.where(db_models.Job.status == status_filter)

    # ``review_deadline ASC NULLS LAST`` keeps the in-review rows that
    # have a real deadline at the top of the list — those are the rows
    # the operator wants to clear first. Terminal-status rows have
    # ``review_deadline IS NULL`` and naturally sink to the bottom.
    stmt = stmt.order_by(
        db_models.Job.review_deadline.asc().nulls_last(),
        db_models.Job.id,
    ).limit(page_size)

    total = int((await session.scalar(count_stmt)) or 0)
    rows = (await session.execute(stmt)).scalars().all()
    return JobListResponse(
        jobs=[_job_row_to_pydantic(r) for r in rows],
        total=total,
    )


@router.post("/{job_id}/approve", response_model=Job)
async def approve_job(
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Flip status to ``approved`` and clear the review deadline."""
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    row = await session.get(db_models.Job, uuid_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    row.status = "approved"
    row.review_deadline = None
    await session.commit()
    return _job_row_to_pydantic(row)


@router.post("/{job_id}/reject", response_model=Job)
async def reject_job(
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Flip status to ``rejected`` and clear the review deadline."""
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    row = await session.get(db_models.Job, uuid_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    row.status = "rejected"
    row.review_deadline = None
    await session.commit()
    return _job_row_to_pydantic(row)
