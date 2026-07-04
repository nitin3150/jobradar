"""Pipeline trigger and status endpoints."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.config import settings
from app.pipeline.graph import run_pipeline
from app.models.pipeline import PipelineRunResponse, PipelineStatusResponse, ScheduleResponse, ScheduleUpdate, SCHEDULE_OPTIONS
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# In-memory pipeline run state (simple approach for single-instance deployment)
_last_run: dict | None = None
_is_running: bool = False

@router.post("/run", response_model=PipelineRunResponse)
async def trigger_pipeline(request: Request):
    """Manually trigger the full LangGraph pipeline."""
    global _last_run, _is_running

    if _is_running:
        return PipelineRunResponse(status="already_running")

    _is_running = True
    try:
        result = await run_pipeline(
            http_client=request.app.state.http_client,
            redis=request.app.state.redis,
            settings=settings,
            browser=getattr(request.app.state, "browser", None),
        )

        _last_run = {
            "timestamp": datetime.now(timezone.utc),
            "stats": result.get("stats", {}),
            "errors": result.get("errors", []),
        }

        return PipelineRunResponse(
            status="completed",
            companies_found=result.get("stats", {}).get("detected", 0),
            companies_saved=result.get("saved_count", 0),
            errors=result.get("errors", []),
            stats=result.get("stats", {}),
        )

    except Exception as e:
        logger.error(f"Pipeline run failed: {e}")
        return PipelineRunResponse(status="failed", errors=[str(e)])
    finally:
        _is_running = False


@router.post("/discover")
async def trigger_discovery(request: Request):
    """Manually run ATS slug discovery (find + attach new company boards)."""
    from app.database import async_session
    from app.pipeline.discovery import run_discovery_pipeline

    try:
        async with async_session() as session:
            attached = await run_discovery_pipeline(
                session,
                request.app.state.http_client,
                browser=getattr(request.app.state, "browser", None),
            )
        return {"status": "completed", "companies_attached": attached}
    except Exception as e:
        logger.error(f"Discovery run failed: {e}")
        return {"status": "failed", "error": str(e)}


@router.get("/schedule", response_model=ScheduleResponse)
async def get_schedule(request: Request):
    """Current job-fetch interval + available options + next run time."""
    from app.scheduler import JOB_FETCH_ID, get_fetch_interval

    interval = await get_fetch_interval(request.app.state.redis)
    next_run = None
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        job = scheduler.get_job(JOB_FETCH_ID)
        next_run = getattr(job, "next_run_time", None) if job else None
    return ScheduleResponse(interval_hours=interval, next_run=next_run)


@router.put("/schedule", response_model=ScheduleResponse)
async def update_schedule(payload: ScheduleUpdate, request: Request):
    """Set how often the job-fetch loop runs. Persists + reschedules live."""
    from fastapi import HTTPException

    from app.scheduler import JOB_FETCH_ID, set_fetch_interval

    if payload.interval_hours not in SCHEDULE_OPTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"interval_hours must be one of {SCHEDULE_OPTIONS}",
        )
    await set_fetch_interval(request.app, payload.interval_hours)

    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job(JOB_FETCH_ID) if scheduler else None
    next_run = getattr(job, "next_run_time", None) if job else None
    return ScheduleResponse(interval_hours=payload.interval_hours, next_run=next_run)


@router.get("/status", response_model=PipelineStatusResponse)
async def pipeline_status():
    """Get pipeline run status."""
    return PipelineStatusResponse(
        is_running=_is_running,
        last_run_at=_last_run["timestamp"] if _last_run else None,
        last_run_stats=_last_run["stats"] if _last_run else None,
        next_scheduled=f"Daily at {settings.pipeline_schedule_hour}:00 {settings.pipeline_schedule_timezone}",
        total_errors=len(_last_run["errors"]) if _last_run else 0,
    )
