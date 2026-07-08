"""Jobs router — review queue for the React ``JobsReview`` page + the
``usePendingCount`` badge widget.

In-memory only; jobs do not survive process restarts. The frontend
expects these specific calls from ``frontend/src/api/jobs.js``:

* ``GET /api/jobs?status=in_review&page_size=50`` →
  ``{"jobs": [...], "total": int}`` — the React page filters
  client-side via :func:`useJobs` so the wire shape is ungrouped. Jobs
  include ``id``, ``status``, ``ats_type``, ``title``, ``company_name``,
  ``url``, ``ai_fit_score``, ``ai_fit_reasoning``, ``review_deadline``.
* ``GET /api/jobs/pending-count`` → ``{"count": int}`` — driven by
  :func:`usePendingCount` for the Navbar badge; the count is the
  number of jobs in ``status == "in_review"`` (matches the *active* tab
  in JobsReview so the badge and the visible count agree).
* ``POST /api/jobs/{job_id}/approve`` — flips ``status`` to
  ``"approved"``; returns the updated Job JSON.
* ``POST /api/jobs/{job_id}/reject`` — flips ``status`` to
  ``"rejected"``; returns the updated Job JSON.

Route-ordering note: ``GET /jobs/pending-count`` is declared BEFORE the
``{{job_id}}`` action routes. Even though ``/pending-count`` shares
no path-pattern with the action routes (different HTTP verb *and* a
suffix that doesn't match ``{job_id}``), declaring it first prevents
future refactors that add ``GET /jobs/{job_id}`` from accidentally
shadowing this literal path.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field


router = APIRouter()


# ---------------------------------------------------------------------------
# Enums — match the React ``STATUS_COLORS`` table in JobsReview.jsx so
# the badge color stays consistent.
# ---------------------------------------------------------------------------
JobStatus = Literal["in_review", "approved", "rejected", "applied", "flagged"]


# ---------------------------------------------------------------------------
# Models — every field the React ``JobsReview.jsx`` reads off
# ``data?.jobs?.map(...)`` is here. ``review_deadline`` is nullable for
# jobs in terminal status (``approved`` / ``rejected`` / ``applied``).
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Seeded store — one record per JobStatus so every filter tab in the
# React page has at least one row, including the ``pending-count``
# badge which counts only ``in_review``. Records are deep-copied on
# ``_seed()`` so test mutations don't bleed back into the canonical list.
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _deadline_in(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


_SEED_RECORDS: list[dict] = [
    {
        "id": "j_1",
        "status": "in_review",
        "ats_type": "ashby",
        "title": "Senior AI Engineer",
        "company_name": "Replicate",
        "url": "https://replicate.com/careers",
        "ai_fit_score": 0.86,
        "ai_fit_reasoning": "Strong match — LLM inference + Python + open-source fluency.",
        "review_deadline": _deadline_in(2),
    },
    {
        "id": "j_2",
        "status": "in_review",
        "ats_type": "lever",
        "title": "Founding Engineer",
        "company_name": "Mastra",
        "url": "https://mastra.ai/careers",
        "ai_fit_score": 0.78,
        "ai_fit_reasoning": "TypeScript + AI agent infrastructure; matches your skill stack.",
        "review_deadline": _deadline_in(5),
    },
    {
        "id": "j_3",
        "status": "approved",
        "ats_type": "greenhouse",
        "title": "Backend Engineer",
        "company_name": "Vercel",
        "url": "https://vercel.com/careers",
        "ai_fit_score": 0.91,
        "ai_fit_reasoning": "High-priority match — Node + edge runtime experience applies directly.",
        "review_deadline": None,
    },
    {
        "id": "j_4",
        "status": "rejected",
        "ats_type": "ashby",
        "title": "Junior ML Engineer",
        "company_name": "Midjourney",
        "url": "https://midjourney.com/careers",
        "ai_fit_score": 0.42,
        "ai_fit_reasoning": "Below your preferred threshold; aligned with your directional interests but lacks senior scope.",
        "review_deadline": None,
    },
    {
        "id": "j_5",
        "status": "applied",
        "ats_type": "greenhouse",
        "title": "Distributed Systems Engineer",
        "company_name": "Cloudflare",
        "url": "https://cloudflare.com/careers",
        "ai_fit_score": 0.74,
        "ai_fit_reasoning": "Applied via apply_worker — Rust + edge experience.",
        "review_deadline": None,
    },
    {
        "id": "j_6",
        "status": "flagged",
        "ats_type": "remotive",
        "title": "Remote Solutions Architect",
        "company_name": "Doist",
        "url": "https://doist.com/careers",
        "ai_fit_score": 0.58,
        "ai_fit_reasoning": "Flagged for manual review — overlaps your stack but the role is IC-track not engineer-track.",
        "review_deadline": None,
    },
]


_JOBS_DB: dict[str, dict] = {}


def _seed() -> None:
    _JOBS_DB.clear()
    for rec in _SEED_RECORDS:
        _JOBS_DB[rec["id"]] = copy.deepcopy(rec)


_seed()


# ---------------------------------------------------------------------------
# Routes — explicit ordering so a future ``GET /jobs/{job_id}`` route
# addition doesn't shadow ``/jobs/pending-count`` by accident.
# ---------------------------------------------------------------------------
@router.get("/pending-count", response_model=PendingCountResponse)
def get_pending_count() -> PendingCountResponse:
    """Number of jobs in the ``in_review`` queue — drives the Navbar badge."""
    count = sum(1 for j in _JOBS_DB.values() if j["status"] == "in_review")
    return PendingCountResponse(count=count)


@router.get("", response_model=JobListResponse)
def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    page_size: int = Query(default=50, ge=1, le=200),
) -> JobListResponse:
    """List jobs with optional ``status`` filter. Newest-review-deadline first."""
    jobs = list(_JOBS_DB.values())
    if status_filter:
        jobs = [j for j in jobs if j["status"] == status_filter]
    jobs.sort(key=_deadline_sort_key)
    page = jobs[:page_size]
    return JobListResponse(
        jobs=[Job(**j) for j in page],
        total=len(jobs),
    )


def _deadline_sort_key(j: dict) -> tuple:
    """Sort key for the deadline-DESC ordering.

    Returns a 3-tuple ``(is_no_deadline, -timestamp, id)``:

    * ``is_no_deadline=True`` sorts AFTER any record with a deadline,
      so jobs in terminal statuses (``approved`` / ``rejected`` /
      ``applied`` / ``flagged``) which have ``review_deadline=None``
      always sink to the bottom of the list.
    * Within the same deadline-bucket, more negative ``-timestamp``
      = larger original timestamp = newer deadline first.
    * ``id`` is a stable tertiary tie-breaker so terminal-status
      records stay in a deterministic order even when their primary
      keys collapse to ``(True, 0)`` (currently every such record
      returns exactly that — without the tertiary we'd be relying on
      Python's stable-sort tie-break on dict-insertion order, which
      can shift if the store is ever rebuilt).
    """
    deadline = j.get("review_deadline")
    if deadline is None:
        # Sentinel sorts strictly after any real timestamp because
        # ``True > False`` and we're using it as the primary key.
        return (True, 0, j["id"])
    ts = datetime.fromisoformat(deadline.replace("Z", "+00:00")).timestamp()
    return (False, -ts, j["id"])


@router.post("/{job_id}/approve", response_model=Job)
def approve_job(job_id: str = Path(min_length=1, max_length=64)) -> Job:
    """Flip status to ``approved`` and clear the review deadline."""
    rec = _JOBS_DB.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    rec["status"] = "approved"
    rec["review_deadline"] = None
    return Job(**rec)


@router.post("/{job_id}/reject", response_model=Job)
def reject_job(job_id: str = Path(min_length=1, max_length=64)) -> Job:
    """Flip status to ``rejected`` and clear the review deadline."""
    rec = _JOBS_DB.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    rec["status"] = "rejected"
    rec["review_deadline"] = None
    return Job(**rec)
