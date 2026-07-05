"""Pipeline trigger and status endpoints."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request

from app.config import settings
from app.pipeline.graph import run_pipeline
from app.schemas.pipeline import PipelineRunResponse, PipelineStatusResponse, ScheduleResponse, ScheduleUpdate, SCHEDULE_OPTIONS
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# In-memory pipeline run state (simple approach for single-instance deployment)
_last_run: dict | None = None
_is_running: bool = False
# Same shape for ATS slug discovery.
_last_discovery: dict | None = None
_is_discovering: bool = False

async def _run_pipeline_task(
    http_client,
    redis,
    settings_obj,
    browser,
) -> None:
    """Background coroutine for POST /api/pipeline/run.

    Runs the LangGraph pipeline to completion in a background task, then
    writes the result into the module-level `_last_run` so /api/pipeline/status
    can surface it. Never propagates — any exception becomes a recorded error
    so /status reflects the failure rather than staying stuck on is_running=True.
    Always flips `_is_running` off in `finally` so the next trigger can run.

    Args are taken positionally (not as a closure over `request`) because
    FastAPI's BackgroundTasks machinery serializes args; closures over
    framework objects can fail to reconstruct in some FastAPI/Starlette
    versions.
    """
    global _last_run, _is_running
    try:
        result = await run_pipeline(
            http_client=http_client,
            redis=redis,
            settings=settings_obj,
            browser=browser,
        )
        _last_run = {
            "timestamp": datetime.now(timezone.utc),
            "stats": result.get("stats", {}),
            "errors": result.get("errors", []),
        }
    except Exception as e:
        logger.error(f"Background pipeline run failed: {e}")
        _last_run = {
            "timestamp": datetime.now(timezone.utc),
            "stats": {},
            "errors": [str(e)],
        }
    finally:
        _is_running = False


@router.post("/run", response_model=PipelineRunResponse)
async def trigger_pipeline(
    background_tasks: BackgroundTasks, request: Request
):
    """Kick off the full LangGraph pipeline and return immediately.

    The pipeline (HTTP scrapers + Apify + LLM scoring + DB writes) takes
    minutes. If we ran it inline, the HTTP connection would stay open past
    browser/curl timeouts (default ~30-60s) and the client would never see
    a response — even if the pipeline ultimately succeeded. So we schedule it
    as a FastAPI BackgroundTask — it runs after the response is sent — and
    return ``status="started"`` right away with no stats.

    Poll GET /api/pipeline/status to track progress: ``is_running`` flips
    False when done, and ``last_run_stats`` / ``last_run_at`` carry the
    final result.
    """
    global _is_running

    if _is_running:
        return PipelineRunResponse(status="already_running")

    _is_running = True
    background_tasks.add_task(
        _run_pipeline_task,
        request.app.state.http_client,
        request.app.state.redis,
        settings,
        getattr(request.app.state, "browser", None),
    )
    return PipelineRunResponse(status="started")


async def _run_discovery_task(http_client, browser) -> None:
    """Background coroutine for POST /api/pipeline/discover.

    Runs `run_discovery_pipeline` to completion in a background task, then
    writes the result into the module-level `_last_discovery` so a future
    discover-status surface can read it. Never propagates — any exception
    becomes a recorded failure. Always flips `_is_discovering` off in
    `finally` so the next trigger can run.

    Args are taken positionally (not as a closure over `request`) because
    FastAPI's BackgroundTasks machinery serializes args; closures over
    framework objects can fail to reconstruct in some FastAPI/Starlette
    versions.
    """
    global _last_discovery, _is_discovering
    try:
        from app.database import async_session
        from app.pipeline.discovery import run_discovery_pipeline

        async with async_session() as session:
            attached = await run_discovery_pipeline(
                session,
                http_client,
                browser=browser,
            )
        _last_discovery = {
            "timestamp": datetime.now(timezone.utc),
            "status": "completed",
            "companies_attached": attached,
        }
    except Exception as e:
        logger.error(f"Background discovery run failed: {e}")
        _last_discovery = {
            "timestamp": datetime.now(timezone.utc),
            "status": "failed",
            "error": str(e),
        }
    finally:
        _is_discovering = False


@router.post("/discover")
async def trigger_discovery(
    background_tasks: BackgroundTasks, request: Request
):
    """Kick off ATS slug discovery and return immediately.

    Discovery does `site:` searches across Google/Bing (via Serper when a
    key is configured, falling back to Playwright) for new company board
    slugs. Those calls routinely take 30s+ on Serper, and minutes under
    the Playwright fallback (CAPTCHA pages, headless-render waits).
    Holding the HTTP connection open that long blows browser/curl timeouts,
    so we schedule the work via FastAPI BackgroundTasks and return
    immediately with `status="started"`. The latest result is written to
    the module-level `_last_discovery` dict; surface it from /status (or a
    future discover-status endpoint).
    """
    global _is_discovering

    if _is_discovering:
        return {"status": "already_running"}

    _is_discovering = True
    background_tasks.add_task(
        _run_discovery_task,
        request.app.state.http_client,
        getattr(request.app.state, "browser", None),
    )
    return {"status": "started"}


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
