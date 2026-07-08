"""Applications router — post-application status tracker for the React
``ApplicationTracker`` page.

In-memory seeded store. Status enum is distinct from the pre-application
``JobStatus`` enum in :mod:`routes.jobs` — applications are POST-submit
(``submitted`` / ``interview`` / ``rejected`` / ``offer`` / ``ghosted``),
jobs are PRE-submit review-queue (``in_review`` / ``approved`` /
``rejected`` / ``applied`` / ``flagged``).

Frontend wire shape (see ``frontend/src/pages/ApplicationTracker.jsx``):

* ``GET /api/applications?status=…&page_size=…`` →
  ``{"applications": [...], "total": int}`` — the React table reads
  ``data?.applications?.map(...)`` so the wire shape is a flat list
  wrapped in an envelope.
* ``PATCH /api/applications/{id}/status`` body
  ``{"status": "...", "notes": "..."}`` (notes optional) — returns
  the updated Application JSON. The frontend invalidates the
  ``['applications']`` cache via ``useQueryClient`` after the
  mutation succeeds; the response body is consumed by mutation
  onSuccess handlers.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field


router = APIRouter()


# ---------------------------------------------------------------------------
# Enums — match the React ``STATUS_COLORS`` table in ApplicationTracker.jsx
# verbatim. Expanding this set requires adding the new color + filter button
# on the frontend too.
# ---------------------------------------------------------------------------
ApplicationStatus = Literal[
    "submitted",
    "interview",
    "rejected",
    "offer",
    "ghosted",
]


# ---------------------------------------------------------------------------
# Models — every field the React ``ApplicationTracker.jsx`` table reads off
# ``data?.applications?.map(...)`` is here. ``last_email_at`` and
# ``submission_screenshot_path`` are nullable because the apply_worker
# hasn't captured them yet for early rows.
# ---------------------------------------------------------------------------
class Application(BaseModel):
    id: str
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


# ---------------------------------------------------------------------------
# Seeded store — 6 demo applications spanning every status so each
# filter button in ApplicationTracker has at least one row. Deep-copied
# on ``_seed()`` so test mutations cannot bleed back into ``_SEED_RECORDS``.
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace(
        "+00:00", "Z"
    )


_SEED_RECORDS: list[dict] = [
    {
        "id": "a_1",
        "job_title": "Senior AI Engineer",
        "company_name": "Replicate",
        "submitted_at": _days_ago(2),
        "status": "submitted",
        "last_email_at": None,
        "submission_screenshot_path": "/api/applications/a_1/screenshot.png",
        "notes": None,
    },
    {
        "id": "a_2",
        "job_title": "Founding Engineer",
        "company_name": "Mastra",
        "submitted_at": _days_ago(4),
        "status": "interview",
        "last_email_at": _days_ago(1),
        "submission_screenshot_path": "/api/applications/a_2/screenshot.png",
        "notes": "First round scheduled Tue 4pm — prep the agent-serving infra deck.",
    },
    {
        "id": "a_3",
        "job_title": "Backend Engineer",
        "company_name": "Vercel",
        "submitted_at": _days_ago(7),
        "status": "rejected",
        "last_email_at": _days_ago(5),
        "submission_screenshot_path": "/api/applications/a_3/screenshot.png",
        "notes": "Recruiter email — they want more distributed-systems depth. Follow up at Q3 cycle.",
    },
    {
        "id": "a_4",
        "job_title": "Distributed Systems Engineer",
        "company_name": "Cloudflare",
        "submitted_at": _days_ago(10),
        "status": "ghosted",
        "last_email_at": _days_ago(8),
        "submission_screenshot_path": None,
        "notes": "No reply after 2 polite follow-ups. Will revisit in 6 months.",
    },
    {
        "id": "a_5",
        "job_title": "Staff Engineer",
        "company_name": "Anthropic",
        "submitted_at": _days_ago(5),
        "status": "interview",
        "last_email_at": _days_ago(2),
        "submission_screenshot_path": "/api/applications/a_5/screenshot.png",
        "notes": "Onsite loop scheduled — system design round Tuesday.",
    },
    {
        "id": "a_6",
        "job_title": "Tech Lead",
        "company_name": "Cursor",
        "submitted_at": _days_ago(1),
        "status": "offer",
        "last_email_at": _days_ago(0),
        "submission_screenshot_path": "/api/applications/a_6/screenshot.png",
        "notes": "Verbal offer received; sign-on bonus + 0.18% equity. Let HM know by Friday.",
    },
]


_APPLICATIONS_DB: dict[str, dict] = {}


def _seed() -> None:
    """Reset :data:`_APPLICATIONS_DB` to deep-copies of the canonical seed."""
    _APPLICATIONS_DB.clear()
    for rec in _SEED_RECORDS:
        _APPLICATIONS_DB[rec["id"]] = copy.deepcopy(rec)


_seed()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=ApplicationListResponse)
def list_applications(
    status_filter: str | None = Query(default=None, alias="status"),
    page_size: int = Query(default=50, ge=1, le=200),
) -> ApplicationListResponse:
    """List applications with optional ``status`` filter. Newest-submitted first."""
    rows = list(_APPLICATIONS_DB.values())
    if status_filter:
        rows = [a for a in rows if a["status"] == status_filter]
    rows.sort(key=lambda a: a["submitted_at"], reverse=True)
    page = rows[:page_size]
    return ApplicationListResponse(
        applications=[Application(**a) for a in page],
        total=len(rows),
    )


@router.patch("/{application_id}/status", response_model=Application)
def patch_application_status(
    payload: ApplicationStatusPatch,
    application_id: str = Path(min_length=1, max_length=64),
) -> Application:
    """Update ``status`` and optionally append a ``notes`` line."""
    rec = _APPLICATIONS_DB.get(application_id)
    if rec is None:
        raise HTTPException(
            status_code=404, detail=f"application {application_id!r} not found"
        )
    rec["status"] = payload.status
    if payload.notes is not None:
        rec["notes"] = payload.notes
    return Application(**rec)
