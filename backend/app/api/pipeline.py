"""Pipeline trigger and status endpoints."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import settings
from app.pipeline.graph import run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# In-memory pipeline run state (simple approach for single-instance deployment)
_last_run: dict | None = None
_is_running: bool = False


class PipelineRunResponse(BaseModel):
    status: str
    companies_found: int = 0
    companies_saved: int = 0
    errors: list[str] = []
    stats: dict = {}


class PipelineStatusResponse(BaseModel):
    is_running: bool
    last_run_at: datetime | None = None
    last_run_stats: dict | None = None
    next_scheduled: str | None = None
    total_errors: int = 0


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
