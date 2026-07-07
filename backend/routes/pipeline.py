"""Pipeline router — HTTP surface for the LangGraph scanner + schedule.

Wraps ``pipeline.graph.scan_pipeline.invoke`` for on-demand full domain
scans (funding/remote/ngos/oss) and ``pipeline.nodes.jobs_boards.runner.run_all``
for boards-specific discovery. Mirrors the React ``ScheduleControl`` +
``StatusTracker`` consumer shape.

Storage is in-process only. Real scheduler ticking is a separate Dockerised
worker concern (``REDIS_URL`` env var) — we do **not** run the worker from
this process; this router is the operator-facing HTTP surface that:
- triggers a one-shot full scan (``POST /run``)
- kicks the boards-only runner (``GET /discover``)
- reads the operator-tuned scan interval (``GET /schedule``)
- updates the scan interval (``PUT /schedule``)
- reports pipeline status (``GET /status``) and dashboard tiles (``GET /stats``).

A concurrency guard (``409 Conflict``) prevents operator-triggered overlapping
scans from blocking the single-worker event loop.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pipeline.graph import scan_pipeline
from pipeline.nodes.jobs_boards.runner import run_all as run_jobs_boards

router = APIRouter()


_log = logging.getLogger("jobradar.pipeline")
IntervalHours = Literal[1, 2, 4, 6, 12, 24]
_INTERVAL_OPTIONS: list[int] = [1, 2, 4, 6, 12, 24]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class PipelineCounts(BaseModel):
    funding: int
    remote: int
    ngos: int
    oss: int
    total: int


class PipelineRunResponse(BaseModel):
    message: str = "True"
    ran_at: str
    duration_seconds: float
    counts: PipelineCounts
    opportunities: dict[str, list[dict]]


class PipelineStatusResponse(BaseModel):
    state: Literal["idle", "running", "error"]
    last_run_at: str | None
    last_run_duration_seconds: float | None
    last_run_counts: PipelineCounts | None
    recent_error: str | None


class DiscoverResponse(BaseModel):
    status: Literal["completed", "failed"]
    companies_attached: int
    scanned: int
    error: str | None = None


class ScheduleResponse(BaseModel):
    interval_hours: int
    options: list[int]
    next_run: str | None
    updated_at: str | None


class ScheduleUpdateRequest(BaseModel):
    interval_hours: IntervalHours


class PipelineStatsResponse(BaseModel):
    total_companies: int
    new_today: int
    high_intent: int
    contacted: int
    ngo_count: int


# --------------------------------------------------------------------------
# In-memory state — mutated in place by ``_reset_state`` so importers keep
# working after a reset (a dict-replace would orphan stale references).
# --------------------------------------------------------------------------
_PIPELINE_STATE: dict = {
    "state": "idle",
    "last_run_at": None,
    "last_run_duration_seconds": None,
    "last_run_counts": None,
    "recent_error": None,
    "interval_hours": 1,
    "schedule_updated_at": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compute_next_run(interval_hours: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=interval_hours)
    ).isoformat().replace("+00:00", "Z")


def _counts_from_langgraph_state(state: dict) -> PipelineCounts:
    funding = state.get("funding") or []
    remote = state.get("remote") or []
    ngos = state.get("ngos") or []
    oss = state.get("oss") or []
    return PipelineCounts(
        funding=len(funding),
        remote=len(remote),
        ngos=len(ngos),
        oss=len(oss),
        total=len(funding) + len(remote) + len(ngos) + len(oss),
    )


def _reset_state() -> None:
    """Reset ``_PIPELINE_STATE`` in place so test setUp() doesn't orphan
    importer references to the dict. Production code never calls this —
    it's purely a test seam.
    """
    _PIPELINE_STATE.clear()
    _PIPELINE_STATE.update({
        "state": "idle",
        "last_run_at": None,
        "last_run_duration_seconds": None,
        "last_run_counts": None,
        "recent_error": None,
        "interval_hours": 1,
        "schedule_updated_at": None,
    })


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.get("/stats", response_model=PipelineStatsResponse)
def get_pipeline_stats() -> PipelineStatsResponse:
    """Aggregate counts for the React StatusTracker tiles.

    ``total_companies`` is sourced from the seeded ``/api/companies`` store
    so the dashboard tile matches the CompanyFeed count; the other tiles
    are zero placeholders until a persistence layer + scheduler land
    (real values will then come from prior-run writes).
    """
    total = 0
    ngo_count = 0
    try:
        # Lazy import — companies may not be loaded at startup ordering.
        from routes.companies import _COMPANIES_DB
        total = len(_COMPANIES_DB)
        ngo_count = sum(
            1 for c in _COMPANIES_DB.values() if c.get("category") == "ngos"
        )
    except ImportError:
        pass
    return PipelineStatsResponse(
        total_companies=total,
        new_today=0,
        high_intent=0,
        contacted=0,
        ngo_count=ngo_count,
    )


@router.get("/status", response_model=PipelineStatusResponse)
def get_pipeline_status() -> PipelineStatusResponse:
    return PipelineStatusResponse(
        state=_PIPELINE_STATE["state"],
        last_run_at=_PIPELINE_STATE["last_run_at"],
        last_run_duration_seconds=_PIPELINE_STATE["last_run_duration_seconds"],
        last_run_counts=_PIPELINE_STATE["last_run_counts"],
        recent_error=_PIPELINE_STATE["recent_error"],
    )


@router.get("/schedule", response_model=ScheduleResponse)
def get_schedule() -> ScheduleResponse:
    return ScheduleResponse(
        interval_hours=_PIPELINE_STATE["interval_hours"],
        options=_INTERVAL_OPTIONS,
        next_run=_compute_next_run(_PIPELINE_STATE["interval_hours"]),
        updated_at=_PIPELINE_STATE["schedule_updated_at"],
    )


@router.put("/schedule", response_model=ScheduleResponse)
def update_schedule(payload: ScheduleUpdateRequest) -> ScheduleResponse:
    # The ``interval_hours: IntervalHours`` Literal already enforces the
    # legal set on Pydantic — invalid values get a 422 automatically.
    _PIPELINE_STATE["interval_hours"] = payload.interval_hours
    _PIPELINE_STATE["schedule_updated_at"] = _now_iso()
    return ScheduleResponse(
        interval_hours=_PIPELINE_STATE["interval_hours"],
        options=_INTERVAL_OPTIONS,
        next_run=_compute_next_run(_PIPELINE_STATE["interval_hours"]),
        updated_at=_PIPELINE_STATE["schedule_updated_at"],
    )


@router.get("/discover", response_model=DiscoverResponse)
def discover() -> DiscoverResponse:
    if _PIPELINE_STATE["state"] == "running":
        raise HTTPException(
            status_code=409,
            detail="pipeline is already running; wait for the current run to finish",
        )
    _PIPELINE_STATE["state"] = "running"
    _PIPELINE_STATE["recent_error"] = None
    try:
        jobs = run_jobs_boards(delta_hours=168)
        return DiscoverResponse(
            status="completed",
            companies_attached=len(jobs),
            scanned=len(jobs),
        )
    except Exception as exc:
        _log.exception("discover run failed")
        _PIPELINE_STATE["recent_error"] = str(exc)
        return DiscoverResponse(
            status="failed",
            companies_attached=0,
            scanned=0,
            error=str(exc),
        )
    finally:
        _PIPELINE_STATE["state"] = "idle"


@router.post("/run", response_model=PipelineRunResponse)
def run_pipeline() -> PipelineRunResponse:
    if _PIPELINE_STATE["state"] == "running":
        raise HTTPException(
            status_code=409,
            detail="pipeline is already running; wait for the current run to finish",
        )
    _PIPELINE_STATE["state"] = "running"
    _PIPELINE_STATE["recent_error"] = None
    started_monotonic = time.monotonic()
    try:
        result = scan_pipeline.invoke({"input": "api"})
    except Exception as exc:
        _log.exception("scan_pipeline.invoke failed")
        _PIPELINE_STATE["state"] = "error"
        _PIPELINE_STATE["recent_error"] = str(exc)
        raise HTTPException(
            status_code=500,
            detail=f"pipeline run failed: {exc}",
        ) from exc
    duration = time.monotonic() - started_monotonic
    counts = _counts_from_langgraph_state(result)
    ran_at = _now_iso()
    opportunities = {
        k: list(result.get(k) or [])
        for k in ("funding", "remote", "ngos", "oss")
    }
    _PIPELINE_STATE["state"] = "idle"
    _PIPELINE_STATE["last_run_at"] = ran_at
    _PIPELINE_STATE["last_run_duration_seconds"] = duration
    _PIPELINE_STATE["last_run_counts"] = counts
    return PipelineRunResponse(
        message="True",
        ran_at=ran_at,
        duration_seconds=duration,
        counts=counts,
        opportunities=opportunities,
    )
