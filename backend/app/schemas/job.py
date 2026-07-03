from datetime import datetime
from uuid import UUID
from pydantic import BaseModel

from app.models.job import JobStatus


class JobResponse(BaseModel):
    id: UUID
    company_id: UUID
    company_name: str | None = None
    title: str
    url: str
    ats_type: str
    jd_text: str | None = None
    ai_fit_score: float | None = None
    ai_fit_reasoning: str | None = None
    status: str
    scraped_at: datetime
    review_deadline: datetime | None = None

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class JobStatusUpdate(BaseModel):
    status: JobStatus
