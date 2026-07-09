"""Jobs router — review queue backed by the real Supabase ``jobs`` table.

The wire shape (and React consumer expectations) is unchanged from
the previous in-memory version; :class:`Job` etc. still match what
``frontend/src/api/jobs.js`` consumes:

* ``GET /api/jobs?status=in_review&page_size=50`` →
  ``{\"jobs\": [...], \"total\": int, \"page\": int, \"page_size\": int}``.
* ``GET /api/jobs/pending-count`` → ``{\"count\": int}`` — number of
  jobs in ``status == \"in_review\"``.
* ``POST /api/jobs/{job_id}/approve`` — flips ``status`` to
  ``\"approved\"`` and clears ``review_deadline``. Bus-compat shim;
  writes a job_status_history row.
* ``POST /api/jobs/{job_id}/reject`` — flips ``status`` to
  ``\"rejected\"`` and clears ``review_deadline``. Bus-compat shim;
  writes a job_status_history row.
* ``PATCH /api/jobs/{job_id}/status`` — canonical status writer. Body
  ``{ status, source?, note? }``. Updates ``jobs.status`` AND inserts
  a ``job_status_history`` row in the same transaction so an analyst
  query can never observe a status with no history.
* ``POST /api/jobs/{job_id}/research`` — sync Interview Prep. Calls
  :class:`services.llm_client.LLMClient.research_opportunity`,
  persists a ``research_reports`` row, returns the report envelope.
* ``GET /api/jobs/{job_id}/research`` — re-open the most recent
  ready report without a fresh LLM call.

Read paths source from :class:`db.models.Job` via the async
SQLAlchemy session factory. Writes from :mod:`services.scoring_service`
land in the same row via an ``INSERT … ON CONFLICT (id) DO UPDATE``
upsert keyed on a deterministic :func:`uuid5`-generated id.

Route-ordering note: ``GET /jobs/pending-count`` is declared BEFORE
the ``{job_id}`` action routes so a future ``GET /jobs/{job_id}``
addition does not shadow the literal pending-count path. The
``GET /jobs/{job_id}/research`` literal is also declared BEFORE
``PATCH /jobs/{job_id}/status`` for the same reason.
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
from services.llm_client import LLMClient
from services.scoring_service import build_profile_summary


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
    # New: board-published timestamps + our DB-side row lifecycle. All
    # nullable because Ashby in particular doesn't expose either on
    # its public scraper endpoints, and ``created_at`` / ``updated_at``
    # are post-0002 columns on the ``jobs`` table.
    posted_at: str | None = None
    source_updated_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class JobListResponse(BaseModel):
    jobs: list[Job]
    total: int
    page: int = 1
    page_size: int = 50


class PendingCountResponse(BaseModel):
    count: int


# ----------------------------------------------------------------------
# PATCH /api/jobs/{id}/status body
# ----------------------------------------------------------------------
class JobStatusPatch(BaseModel):
    """Body for the canonical status writer.

    ``status`` is required; ``source`` defaults to ``"user"`` so an
    operator-click is the audit-trail default. ``note`` is optional
    and bounded to 2 KB so a runaway note cannot balloon row sizes
    (the JobStatusHistory table stores it as TEXT without a length cap
    at the DB layer).
    """

    status: JobStatus
    source: str | None = Field(default="user", max_length=64)
    note: str | None = Field(default=None, max_length=2000)


# ----------------------------------------------------------------------
# Research report envelope
# ----------------------------------------------------------------------
class ResearchReport(BaseModel):
    id: str
    job_id: str | None
    status: str  # "ready" | "failed" | "pending"
    content: str | None
    model_used: str | None
    error: str | None
    requested_at: str
    generated_at: str | None


# ----------------------------------------------------------------------
# Translation helpers — DB row → Pydantic wire shape
# ----------------------------------------------------------------------
def _iso_utc(dt: datetime | None) -> str | None:
    """Render a timezone-aware datetime as ISO 8601 with a trailing ``Z``.

    Same ``+00:00`` → ``Z`` rewrite the rest of JobRadar's wire
    format uses, so frontend code that does ``new Date(s).toISOString()``
    comparison can rely on the suffix.
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_row_to_pydantic(row: db_models.Job) -> Job:
    """Map an ORM ``Job`` row to the wire-shape Pydantic ``Job``.

    All datetime fields render through :func:`_iso_utc` for the
    trailing-``Z`` rewrite. ``id`` is a real Postgres UUID but the
    contract is a string.
    """
    return Job(
        id=str(row.id),
        status=row.status,
        ats_type=row.ats_type,
        title=row.title,
        company_name=row.company_name,
        url=row.url,
        ai_fit_score=row.ai_fit_score,
        ai_fit_reasoning=row.ai_fit_reasoning,
        review_deadline=_iso_utc(row.review_deadline),
        posted_at=_iso_utc(row.posted_at),
        source_updated_at=_iso_utc(row.source_updated_at),
        created_at=_iso_utc(row.created_at),
        updated_at=_iso_utc(row.updated_at),
    )


def _research_row_to_pydantic(row: db_models.ResearchReport) -> ResearchReport:
    return ResearchReport(
        id=str(row.id),
        job_id=str(row.job_id) if row.job_id is not None else None,
        status=row.status,
        content=row.content,
        model_used=row.model_used,
        error=row.error,
        requested_at=_iso_utc(row.requested_at) or "",
        generated_at=_iso_utc(row.generated_at),
    )


def record_status_history(
    session: AsyncSession,
    job_id: UUID,
    from_status: str | None,
    to_status: str,
    source: str,
    note: str | None,
) -> db_models.JobStatusHistory:
    """Append a job_status_history row in the *current* session.

    The caller is responsible for ``session.commit()`` so the history
    row and the parent ``jobs.status`` update land in the same
    transaction. Splitting them would let a future observer see a
    status change with no history — which is exactly the bug the
    v0.5 audit-trail rebuild is meant to prevent.
    """
    history = db_models.JobStatusHistory(
        job_id=job_id,
        from_status=from_status,
        to_status=to_status,
        source=source or db_models.JOB_STATUS_SOURCE_USER,
        note=note,
    )
    session.add(history)
    return history


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
    """Deterministic UUID for a seed marker (``"j_1"`` → stable UUID).

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
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Single status (e.g. ``in_review``) OR comma-separated list "
            "(e.g. ``in_review,approved``) for a multi-status OR query. "
            "Unknown values short-circuit to an empty result set."
        ),
    ),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=50, ge=1, le=200),
    q: str | None = Query(default=None, max_length=200),
    ats_type: str | None = Query(default=None, max_length=32),
    score_min: float = Query(default=0.0, ge=0.0, le=1.0),
    score_max: float = Query(default=1.0, ge=0.0, le=1.0),
    posted_from: str | None = Query(default=None, max_length=32),
    posted_to: str | None = Query(default=None, max_length=32),
    session: AsyncSession = Depends(get_session),
) -> JobListResponse:
    """List jobs with optional ``status`` filter, server-side pagination,
    free-text search across title + company_name, ats_type source
    filter, score range (floor + ceiling), and posted-date range.

    The new envelope includes ``page`` + ``page_size`` so the React
    JobBoard can render the prev/next controls + the "showing N of M"
    counter. ``total`` reflects the matched set *before* slicing.

    ``q`` is a case-insensitive ILIKE across ``title`` and
    ``company_name``. Posted dates are ISO 8601 strings (YYYY-MM-DD);
    the route parses them once and re-uses for both bounds.

    ``status`` accepts a single value (``?status=in_review``) OR a
    comma-separated list (``?status=in_review,approved``) for a
    multi-status OR query. Unknown values in either form short-circuit
    to an empty result set so a typo at the caller doesn't reach the
    SQLAlchemy ``jobs.status = $1::job_status`` comparison, where
    ``<anything>`` would raise ``invalid input value for enum
    job_status: ...``.

    ``score_min``/``score_max`` together form a half-open / closed
    range filter. Default ``(0.0, 1.0)`` is a no-op; the React
    JobBoard uses a slider that emits both bounds.
    """
    stmt = select(db_models.Job)
    count_stmt = select(func.count(db_models.Job.id))

    if status_filter:
        # Comma-separated list, with empty fragments dropped. Any
        # unknown fragment short-circuits to empty (same contract as
        # the single-status path).
        requested_statuses = [
            s.strip() for s in status_filter.split(",") if s.strip()
        ]
        if not requested_statuses:
            return JobListResponse(jobs=[], total=0, page=page, page_size=page_size)
        unknown = [s for s in requested_statuses if s not in JOB_STATUS_VALUES]
        if unknown:
            return JobListResponse(jobs=[], total=0, page=page, page_size=page_size)
        # ``Job.status.in_(...)`` is the idiomatic SQLAlchemy
        # expression for both the single- and multi-status case —
        # it expands to ``status = ANY($1)`` which the planner treats
        # identically to a single ``status = $1`` predicate when the
        # array has one element, and to an OR of equals when the
        # array has many. The ``idx_jobs_status_created`` index covers
        # the lookup either way. No need for a single-vs-multi branch.
        stmt = stmt.where(db_models.Job.status.in_(requested_statuses))
        count_stmt = count_stmt.where(db_models.Job.status.in_(requested_statuses))

    if ats_type:
        stmt = stmt.where(db_models.Job.ats_type == ats_type)
        count_stmt = count_stmt.where(db_models.Job.ats_type == ats_type)

    if score_min > 0.0:
        stmt = stmt.where(db_models.Job.ai_fit_score >= score_min)
        count_stmt = count_stmt.where(db_models.Job.ai_fit_score >= score_min)
    if score_max < 1.0:
        stmt = stmt.where(db_models.Job.ai_fit_score <= score_max)
        count_stmt = count_stmt.where(db_models.Job.ai_fit_score <= score_max)

    if q:
        # ILIKE on both fields. Wrap with ``%`` wildcards so a partial
        # match (no anchored start) is cheap; an index on either field
        # wouldn't help a ``%foo%`` query anyway, so we don't need
        # to add one for this access pattern.
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(db_models.Job.title).like(like)
            | func.lower(db_models.Job.company_name).like(like)
        )
        count_stmt = count_stmt.where(
            func.lower(db_models.Job.title).like(like)
            | func.lower(db_models.Job.company_name).like(like)
        )

    if posted_from:
        try:
            posted_from_dt = datetime.fromisoformat(posted_from)
            if posted_from_dt.tzinfo is None:
                posted_from_dt = posted_from_dt.replace(tzinfo=timezone.utc)
            stmt = stmt.where(db_models.Job.posted_at >= posted_from_dt)
            count_stmt = count_stmt.where(db_models.Job.posted_at >= posted_from_dt)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"posted_from={posted_from!r} is not a valid ISO 8601 date",
            ) from exc

    if posted_to:
        try:
            posted_to_dt = datetime.fromisoformat(posted_to)
            if posted_to_dt.tzinfo is None:
                posted_to_dt = posted_to_dt.replace(tzinfo=timezone.utc)
            stmt = stmt.where(db_models.Job.posted_at <= posted_to_dt)
            count_stmt = count_stmt.where(db_models.Job.posted_at <= posted_to_dt)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"posted_to={posted_to!r} is not a valid ISO 8601 date",
            ) from exc

    # ``review_deadline ASC NULLS LAST`` keeps the in-review rows that
    # have a real deadline at the top of the list — those are the rows
    # the operator wants to clear first. Terminal-status rows have
    # ``review_deadline IS NULL`` and naturally sink to the bottom.
    stmt = stmt.order_by(
        db_models.Job.review_deadline.asc().nulls_last(),
        db_models.Job.id,
    ).offset((page - 1) * page_size).limit(page_size)

    total = int((await session.scalar(count_stmt)) or 0)
    rows = (await session.execute(stmt)).scalars().all()
    return JobListResponse(
        jobs=[_job_row_to_pydantic(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{job_id}/approve", response_model=Job)
async def approve_job(
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Flip status to ``approved`` and clear the review deadline.

    Bus-compat shim around :func:`patch_job_status`. New UI should
    call ``PATCH /api/jobs/{id}/status`` directly; this shim exists
    for the legacy ``useApproveJob`` hook and any external automation
    that still POSTs here. The history write happens in the same
    session as the status update so the two writes commit together.
    """
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    row = await session.get(db_models.Job, uuid_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    previous_status = row.status
    row.status = "approved"
    row.review_deadline = None
    record_status_history(session, row.id, previous_status, "approved", "user", None)
    await session.commit()
    return _job_row_to_pydantic(row)


@router.post("/{job_id}/reject", response_model=Job)
async def reject_job(
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Flip status to ``rejected`` and clear the review deadline.

    Bus-compat shim around :func:`patch_job_status`; same history-
    write contract as :func:`approve_job`.
    """
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    row = await session.get(db_models.Job, uuid_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    previous_status = row.status
    row.status = "rejected"
    row.review_deadline = None
    record_status_history(session, row.id, previous_status, "rejected", "user", None)
    await session.commit()
    return _job_row_to_pydantic(row)


@router.patch("/{job_id}/status", response_model=Job)
async def patch_job_status(
    payload: JobStatusPatch,
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> Job:
    """Canonical status writer. Updates ``jobs.status`` AND inserts a
    ``job_status_history`` row in the same transaction so a future
    audit query can never observe a status with no history.

    The ``source`` column on the history row defaults to ``"user"``
    (operator click). Future automated paths (the
    ``auto_apply_worker`` blueprint) will write ``source="auto_apply"``
    so a single query surfaces the difference.

    Validation: ``status`` must be one of the five valid ``JobStatus``
    enum values; ``source`` is free-text but bounded to 64 chars;
    ``note`` is bounded to 2 KB. Pydantic raises 422 on bad input.
    """
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    row = await session.get(db_models.Job, uuid_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")

    previous_status = row.status
    row.status = payload.status
    # ``approved`` / ``rejected`` / ``applied`` are terminal — clear
    # the review deadline so the in-review queue's partial index
    # stops surfacing this row. ``in_review`` / ``flagged`` keep
    # whatever deadline the prior transition left.
    if payload.status in ("approved", "rejected", "applied"):
        row.review_deadline = None

    record_status_history(
        session,
        row.id,
        previous_status,
        payload.status,
        payload.source or db_models.JOB_STATUS_SOURCE_USER,
        payload.note,
    )
    await session.commit()
    return _job_row_to_pydantic(row)


@router.post("/{job_id}/research", response_model=ResearchReport)
async def post_research(
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> ResearchReport:
    """Sync Interview Prep. Loads the job, calls
    :func:`LLMClient.research_opportunity`, persists a
    ``research_reports`` row, returns the envelope.

    The LLM call is heavy (15-60s); a future async UX (a
    ``research_reports`` row inserted with ``status='pending'`` then
    a separate worker that flips it) can land on top of this shape
    without changing the persistence model.
    """
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    row = await session.get(db_models.Job, uuid_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")

    now = datetime.now(timezone.utc)
    try:
        client = LLMClient.from_env()
        # Compose a Job-shaped dict for ``research_opportunity`` from
        # the ORM row. We don't have a description column on Job so
        # the brief is built from title + company + URL + AI reasoning
        # — the operator's profile summary goes through unchanged.
        job_payload = {
            "id": str(row.id),
            "title": row.title,
            "company_name": row.company_name,
            "url": row.url,
            "ats_type": row.ats_type,
            "description": row.ai_fit_reasoning or "",
        }
        profile_summary = build_profile_summary()
        content, model_used = await client.research_opportunity(
            job_payload, profile_summary
        )
        report = db_models.ResearchReport(
            job_id=row.id,
            status=db_models.RESEARCH_STATUS_READY,
            content=content,
            model_used=model_used,
            error=None,
            requested_at=now,
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 — RuntimeError is a subclass
        # ``LLMClient.from_env()`` raised — no API key configured. Or
        # ``research_opportunity`` raised — every provider failed. Or
        # any transient provider exception. Either way we persist a
        # ``failed`` row and return 502 so the React modal can
        # surface the error verbatim. Catching ``Exception`` (with
        # the BLE001 noqa) means a stray programming error still
        # gets the failed-row + 502 treatment rather than a 500
        # stacktrace the operator has to grep worker logs for.
        report = db_models.ResearchReport(
            job_id=row.id,
            status=db_models.RESEARCH_STATUS_FAILED,
            content=None,
            model_used=None,
            error=str(exc),
            requested_at=now,
            generated_at=datetime.now(timezone.utc),
        )
        session.add(report)
        await session.commit()
        raise HTTPException(
            status_code=502,
            detail=f"research failed: {exc}",
        ) from exc

    session.add(report)
    await session.commit()
    return _research_row_to_pydantic(report)


@router.get("/{job_id}/research", response_model=ResearchReport)
async def get_latest_research(
    job_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> ResearchReport:
    """Re-open the most recent ready report for a job without paying
    for a fresh LLM call.

    Returns 404 when no report exists yet so the React modal can
    drive a fresh ``POST /api/jobs/{id}/research`` from a 404 catch.
    """
    try:
        uuid_id = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found") from exc

    stmt = (
        select(db_models.ResearchReport)
        .where(db_models.ResearchReport.job_id == uuid_id)
        .where(db_models.ResearchReport.status == db_models.RESEARCH_STATUS_READY)
        .order_by(db_models.ResearchReport.requested_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no research report for job {job_id!r} yet",
        )
    return _research_row_to_pydantic(row)
