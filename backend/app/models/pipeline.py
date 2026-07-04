from pydantic import BaseModel
from datetime import datetime

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

SCHEDULE_OPTIONS = [1, 2, 4, 6, 12, 24]

class ScheduleResponse(BaseModel):
    interval_hours: int
    options: list[int] = SCHEDULE_OPTIONS
    next_run: datetime | None = None


class ScheduleUpdate(BaseModel):
    interval_hours: int