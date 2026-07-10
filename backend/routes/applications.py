"""Applications router — post-application status tracker for the React
``ApplicationTracker`` page, backed by the real Supabase ``applications``
table.

The wire shape (and React consumer expectations) is unchanged from
the previous in-memory version; :class:`Application` etc. still match
what ``frontend/src/pages/ApplicationTracker.jsx`` and
``frontend/src/hooks/useApplications.js`` consume:

* ``GET /api/applications?status=…&page_size=…`` →
  ``{"applications": [...], "total": int}`` — the React table reads
  ``data?.applications?.map(...)`` so the wire shape is a flat list
  wrapped in an envelope.
* ``POST /api/applications`` body ``{"job_id": "...", "notes": "..."}``
  (notes optional) — creates an :class:`Application` row with
  ``status="submitted"`` and atomically flips the linked
  :class:`db.models.Job` row's ``status`` from ``"approved"`` to
  ``"applied"``. The response is the new :class:`Application` JSON.
  This is the manual-apply handoff endpoint — the React
  ``JobsReview`` card's **Mark as applied** button posts here after
  the operator opens the job URL in a new tab and applies externally.
* ``PATCH /api/applications/{id}/status`` body
  ``{"status": "...", "notes": "..."}`` (notes optional) — returns
  the updated Application JSON. The frontend invalidates the
  ``['applications']`` cache via ``useQueryClient`` after the
  mutation succeeds; the response body is consumed by mutation
  onSuccess handlers.

Read paths source from :class:`db.models.Application` via the async
SQLAlchemy session factory. State transitions are atomic within a
single ``session.commit()`` so a partial failure (e.g. an FK
violation on insert) cannot leave the system in the bad state of
"job is ``applied`` but no :class:`Application` row exists".

State machine for the POST endpoint:

* Job row must exist (else 404).
* Job row's ``status`` must currently be ``"approved"`` (else 409).
  This is the only valid pre-condition for marking the job applied
  — the operator's review decision was the gate. Submitting a job
  still in ``"in_review"`` is rejected so the review queue can't
  be silently bypassed.
* After a successful POST the job row is ``"applied"`` and an
  :class:`Application` row with ``status="submitted"`` exists for
  it. A second POST returns 409 (idempotency guard against a
  accidental double-click of the **Mark as applied** button).

Route-ordering note: the literal ``GET /api/applications`` route
is declared BEFORE the ``{application_id}`` action routes so a
future ``GET /api/applications/{application_id}`` addition does
not shadow the list path.
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
from db.audit import record_status_history
from db.session import get_session, require_database_configured


router = APIRouter()


# At the very first request, surface a clear mis-config error instead
# of a confusing asyncpg stack trace. Cheap to call — module-level.
require_database_configured()


# ---------------------------------------------------------------------------
# Enums + Pydantic wire shape
# ---------------------------------------------------------------------------
ApplicationStatus = Literal[
    "submitted",
    "interview",
    "rejected",
    "offer",
    "ghosted",
]

# Single source of truth: read the Literal's string members at import
# time so a future ``ApplicationStatus`` expansion automatically widens
# this guard set without a second hand-edited list to keep in sync.
# Used in :func:`list_applications` as an allow-list so an unknown
# ``?status=<anything>`` query short-circuits to an empty result
# instead of reaching the SQLAlchemy
# ``applications.status = $1::application_status`` comparison, where
# ``<anything>`` is not a valid enum value and Postgres raises
# ``invalid input value for enum application_status: ...``.
APPLICATION_STATUS_VALUES: frozenset[str] = frozenset(get_args(ApplicationStatus))


class Application(BaseModel):
    id: str
    job_id: str | None = None
    job_title: str
    company_name: str
    submitted_at: str       # ISO 8601 UTC
    status: ApplicationStatus
    last_email_at: str | None = None  # ISO 8601 UTC or None
    submission_screenshot_path: str | None = None  # URL or None
    notes: str | None = None


class ApplicationListResponse(BaseModel):
    applications: list[Application]
    total: int


class ApplicationStatusPatch(BaseModel):
    status: ApplicationStatus
    notes: str | None = Field(default=None, max_length=2000)


class CreateApplicationRequest(BaseModel):
    """Body for ``POST /api/applications``.

    ``job_id`` is the only required field. ``notes`` is optional and
    propagates to the new :class:`Application` row verbatim so the
    operator can record "Applied via LinkedIn / Referred by John" at
    the same time as the apply handoff. ``submission_screenshot_path``
    is intentionally NOT exposed — manual-apply flows don't produce a
    Playwright screenshot; the apply_worker (out of scope) will be the
    sole writer of that column when it lands.
    """

    job_id: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Translation helpers — DB row → Pydantic wire shape
# ---------------------------------------------------------------------------
def _iso_utc(dt: datetime | None) -> str | None:
    """Render a timezone-aware datetime as ISO 8601 with a trailing ``Z``.

    Mirrors :func:`routes.jobs._job_row_to_pydantic`'s rewrite so the
    wire format stays consistent across the jobs and applications
    routers — tests and the React frontend match on the ``Z`` suffix
    rather than ``+00:00``.
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _application_row_to_pydantic(row: db_models.Application) -> Application:
    """Map an ORM ``Application`` row to the wire-shape Pydantic model.

    Two non-trivial conversions: ``id`` is a real Postgres UUID but
    the contract is a string, and ``job_id`` is nullable (a
    manual-apply against a long-deleted job leaves the FK dangling by
    design — see ``db/models.py``'s ``ON DELETE SET NULL`` clause).
    Datetime fields are rendered with the same ``+00:00`` ⟶ ``Z``
    rewrite the rest of JobRadar's wire format uses.
    """
    return Application(
        id=str(row.id),
        job_id=str(row.job_id) if row.job_id is not None else None,
        job_title=row.job_title,
        company_name=row.company_name,
        submitted_at=_iso_utc(row.submitted_at) or "",
        status=row.status,
        last_email_at=_iso_utc(row.last_email_at),
        submission_screenshot_path=row.submission_screenshot_path,
        notes=row.notes,
    )


# ---------------------------------------------------------------------------
# Seeding helper — test seam only. Production never calls this.
# Mirrors the pattern in :mod:`routes.jobs` (``_seed_job_rows``) so
# test_applications can install a known fixture in place of the
# previous in-memory dict.
# ---------------------------------------------------------------------------
_TEST_SEED_RECORDS_RAW: list[dict] = [
    {
        "id_uuid_text": "a_1",
        "status": "submitted",
        "days_ago": 2,
        "job_title": "Senior AI Engineer",
        "company_name": "Replicate",
        "last_email_at_days_ago": None,
        "submission_screenshot_path": "/api/applications/a_1/screenshot.png",
        "notes": None,
    },
    {
        "id_uuid_text": "a_2",
        "status": "interview",
        "days_ago": 4,
        "job_title": "Founding Engineer",
        "company_name": "Mastra",
        "last_email_at_days_ago": 1,
        "submission_screenshot_path": "/api/applications/a_2/screenshot.png",
        "notes": "First round scheduled Tue 4pm — prep the agent-serving infra deck.",
    },
    {
        "id_uuid_text": "a_3",
        "status": "rejected",
        "days_ago": 7,
        "job_title": "Backend Engineer",
        "company_name": "Vercel",
        "last_email_at_days_ago": 5,
        "submission_screenshot_path": "/api/applications/a_3/screenshot.png",
        "notes": "Recruiter email — they want more distributed-systems depth. Follow up at Q3 cycle.",
    },
    {
        "id_uuid_text": "a_4",
        "status": "ghosted",
        "days_ago": 10,
        "job_title": "Distributed Systems Engineer",
        "company_name": "Cloudflare",
        "last_email_at_days_ago": 8,
        "submission_screenshot_path": None,
        "notes": "No reply after 2 polite follow-ups. Will revisit in 6 months.",
    },
    {
        "id_uuid_text": "a_5",
        "status": "interview",
        "days_ago": 5,
        "job_title": "Staff Engineer",
        "company_name": "Anthropic",
        "last_email_at_days_ago": 2,
        "submission_screenshot_path": "/api/applications/a_5/screenshot.png",
        "notes": "Onsite loop scheduled — system design round Tuesday.",
    },
    {
        "id_uuid_text": "a_6",
        "status": "offer",
        "days_ago": 1,
        "job_title": "Tech Lead",
        "company_name": "Cursor",
        "last_email_at_days_ago": 0,
        "submission_screenshot_path": "/api/applications/a_6/screenshot.png",
        "notes": "Verbal offer received; sign-on bonus + 0.18% equity. Let HM know by Friday.",
    },
]


_SEED_NAMESPACE = UUID("12345678-1234-5678-1234-567812345678")


def _seed_id_for(marker: str) -> UUID:
    """Deterministic UUID for a seed marker (``"a_1"`` ⟶ stable UUID).

    Duplicated from :mod:`routes.jobs._seed_id_for` rather than
    imported so the two routers can evolve their seed schemas
    independently. Same fixed namespace (a no-op-rendering UUID) so
    the test can reason about ``_seed_id_for("a_1")`` as a stable
    primary key across runs without an external fixture file.
    """
    return uuid5(_SEED_NAMESPACE, marker)


async def _seed_applications(session: AsyncSession) -> None:
    """Truncate ``applications`` then insert the canonical seed fixture.

    Tests call this in ``setUp`` — production never does. The
    ``job_id`` FK is left NULL for seeded rows so this seed can run
    against a freshly-migrated DB without requiring a parallel
    ``jobs`` seed; the new POST endpoint exercises the FK path
    explicitly.
    """
    from sqlalchemy import delete as sa_delete

    now = datetime.now(timezone.utc)

    await session.execute(sa_delete(db_models.Application))
    await session.flush()

    for raw in _TEST_SEED_RECORDS_RAW:
        submitted_at = now - timedelta(days=raw["days_ago"])
        last_email_at = (
            now - timedelta(days=raw["last_email_at_days_ago"])
            if raw["last_email_at_days_ago"] is not None
            else None
        )
        row = db_models.Application(
            id=_seed_id_for(raw["id_uuid_text"]),
            job_id=None,  # seeded rows intentionally orphaned from jobs
            job_title=raw["job_title"],
            company_name=raw["company_name"],
            submitted_at=submitted_at,
            status=raw["status"],
            last_email_at=last_email_at,
            submission_screenshot_path=raw["submission_screenshot_path"],
            notes=raw["notes"],
        )
        session.add(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=ApplicationListResponse)
async def list_applications(
    status_filter: str | None = Query(default=None, alias="status"),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> ApplicationListResponse:
    """List applications with optional ``status`` filter. Newest-submitted first."""
    stmt = select(db_models.Application)
    count_stmt = select(func.count(db_models.Application.id))
    if status_filter:
        # Same unknown-status short-circuit as :func:`routes.jobs.list_jobs`
        # — letting an arbitrary client string reach PG would raise
        # ``invalid input value for enum application_status: '...'``.
        # Returning an empty envelope (200 + []) matches the wire
        # shape the React ``useApplications`` hook already renders.
        if status_filter not in APPLICATION_STATUS_VALUES:
            return ApplicationListResponse(applications=[], total=0)
        stmt = stmt.where(db_models.Application.status == status_filter)
        count_stmt = count_stmt.where(db_models.Application.status == status_filter)

    # Newest-submitted first. The :class:`db.models.Application`
    # ``idx_applications_status_submitted`` partial composite index
    # on ``(status, submitted_at DESC)`` keeps this cheap when the
    # status filter is supplied; without the filter the planner
    # falls back to a full scan but the table is expected to stay
    # small (hundreds of rows, not millions) under the manual-apply
    # workflow.
    stmt = stmt.order_by(
        db_models.Application.submitted_at.desc(),
        db_models.Application.id,
    ).limit(page_size)

    total = int((await session.scalar(count_stmt)) or 0)
    rows = (await session.execute(stmt)).scalars().all()
    return ApplicationListResponse(
        applications=[_application_row_to_pydantic(r) for r in rows],
        total=total,
    )


@router.post("", response_model=Application, status_code=201)
async def create_application_from_job(
    payload: CreateApplicationRequest,
    session: AsyncSession = Depends(get_session),
) -> Application:
    """Create an :class:`Application` row + flip the linked :class:`Job`
    to ``status="applied"`` in one transaction.

    **State machine guard.** The job's current ``status`` must be
    ``"approved"`` — the operator's review decision is the gate. A
    job in any other state (``in_review``, ``rejected``, ``applied``,
    ``flagged``) is rejected with 409 so the review queue can't be
    silently bypassed and an accidental double-click on **Mark as
    applied** surfaces as a clear error.

    **Atomicity.** The :class:`Application` ``session.add()`` and the
    ``Job.status = "applied"`` attribute set both live inside the
    same ``session`` / ``session.commit()`` call. A failure in either
    the INSERT or the UPDATE causes the whole transaction to roll
    back, so a partial-failure state (e.g. job marked applied but
    no application row) is impossible.

    **Race-condition note.** Two concurrent POSTs for the same
    ``job_id`` could both pass the "is approved?" check before
    either commits; both would then insert an Application row. For
    a single-operator manual flow this is acceptable. The mitigation
    when a real concurrency need arises is a partial unique index
    on ``applications(job_id) WHERE job_id IS NOT NULL`` — a 5-line
    migration that turns the second insert into a 23505 violation
    we can map to 409. Out of scope for v1.
    """
    # 1. Parse the job_id into a UUID. Bad UUID format is treated as
    # "not found" rather than 422 because the "did this row exist?"
    # check is the primary failure mode we want to surface in the
    # operator log — same convention as :func:`routes.jobs.approve_job`.
    try:
        job_uuid = UUID(payload.job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"job {payload.job_id!r} not found"
        ) from exc

    # 2. Load the Job row. ``session.get()`` uses the primary key and
    # raises an exception on missing — we wrap the result in a None
    # check so the 404 message is well-formed.
    job_row = await session.get(db_models.Job, job_uuid)
    if job_row is None:
        raise HTTPException(
            status_code=404, detail=f"job {payload.job_id!r} not found"
        )

    # 3. State machine guard — only approved jobs can transition to applied.
    # ``paused`` is a special case so the error message tells the
    # operator exactly how to fix it (un-park the job first) rather
    # than generically rejecting with "wrong status". Without this
    # branch an operator who paused a row to read a relocation clause
    # sees "wrong status" and has to grep the codebase to remember
    # the recovery is PATCH /api/jobs/{id}/status back to "approved".
    if job_row.status == "paused":
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {payload.job_id!r} is paused; "
                f"resume it to status 'approved' before marking it applied "
                f"(PATCH /api/jobs/{payload.job_id}/status with status='approved')"
            ),
        )
    if job_row.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {payload.job_id!r} is in status {job_row.status!r}; "
                f"only jobs with status 'approved' can be marked as applied"
            ),
        )

    # 4. Build the new Application row. ``submitted_at`` is the
    # moment the operator clicked **Mark as applied** (== the
    # manual apply handoff), not the moment the boards runner
    # first surfaced the job. The screenshot column stays NULL —
    # manual-apply flows don't produce one; the apply_worker (out
    # of scope for v1) is the only writer.
    now = datetime.now(timezone.utc)
    app_row = db_models.Application(
        job_id=job_row.id,
        job_title=job_row.title,
        company_name=job_row.company_name,
        submitted_at=now,
        status="submitted",
        last_email_at=None,
        submission_screenshot_path=None,
        notes=payload.notes,
    )
    session.add(app_row)

    # 5. Flip the Job's status in the same session. The attribute
    # assignment is tracked by SQLAlchemy's unit-of-work; the
    # session.add() above is too. Both land in the same INSERT/
    # UPDATE pair at session.commit() time.
    previous_job_status = job_row.status
    job_row.status = "applied"
    # Audit-trail row: same transaction as the INSERT above + the
    # status flip. The helper writes a ``job_status_history`` row
    # with ``source="user"`` (the operator's manual-apply click is
    # the audit default) and the optional ``notes`` carried
    # through from the POST body so the operator's "applied via
    # LinkedIn / referred by John" annotation lives next to the
    # transition in the history table. Same atomicity guarantee
    # as the PATCH ``/api/jobs/{id}/status`` path: a future
    # observer can never see ``status='applied'`` without a
    # matching history row.
    #
    # The helper is imported at module-load from :mod:`db.audit`
    # (alongside the matching call from :mod:`routes.jobs`), so a
    # future third router that also writes a status transition can
    # use the same top-level import without copy-pasting the
    # inline ``session.add(...)`` block. There is no longer a
    # lazy-import dance here: the original concern was a circular
    # chain through ``routes.jobs``; routing the helper through
    # ``db.audit`` instead breaks the cycle at its root because
    # ``db.audit`` depends on ``db.models`` and the AsyncSession
    # type only — no FastAPI / routes imports.
    record_status_history(
        session,
        job_row.id,
        previous_job_status,
        "applied",
        db_models.JOB_STATUS_SOURCE_USER,
        payload.notes,
    )

    # 6. Single commit. Any failure between the add() and the
    # commit() (e.g. the FK constraint check on the INSERT) rolls
    # back BOTH writes so the system never lands in the "job is
    # applied but no application exists" bad state.
    await session.commit()

    # 7. Refresh the row so the response carries the server-side
    # defaults (id, created_at) populated by Postgres rather than
    # ``None``. ``expire_on_commit=False`` on the session factory
    # means the in-memory copy is the truth post-commit, so the
    # translation is straightforward.
    await session.refresh(app_row)
    return _application_row_to_pydantic(app_row)


@router.patch("/{application_id}/status", response_model=Application)
async def patch_application_status(
    payload: ApplicationStatusPatch,
    application_id: str = Path(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> Application:
    """Update ``status`` and optionally append a ``notes`` line."""
    try:
        app_uuid = UUID(application_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"application {application_id!r} not found"
        ) from exc

    row = await session.get(db_models.Application, app_uuid)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"application {application_id!r} not found"
        )
    row.status = payload.status
    if payload.notes is not None:
        row.notes = payload.notes
    await session.commit()
    return _application_row_to_pydantic(row)
