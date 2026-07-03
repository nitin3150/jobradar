from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class ApplicationResponse(BaseModel):
    id: UUID
    job_id: UUID
    job_title: str | None = None
    company_name: str | None = None
    submitted_at: datetime
    submission_screenshot_path: str | None = None
    status: str
    gmail_thread_id: str | None = None
    last_email_at: datetime | None = None
    notes: str | None = None

    model_config = {"from_attributes": True}


class ApplicationListResponse(BaseModel):
    applications: list[ApplicationResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ApplicationStatusUpdate(BaseModel):
    status: str
    notes: str | None = None
