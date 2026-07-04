from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ResumeResponse(BaseModel):
    id: UUID
    name: str
    content_type: str
    size_bytes: int
    tags: list[str] = Field(default_factory=list)
    is_default: bool
    uploaded_at: datetime
    # Convenience: relative URL the frontend can hit to download the file.
    download_url: str

    model_config = {"from_attributes": True}


class ResumeListResponse(BaseModel):
    resumes: list[ResumeResponse]


class ResumeUpdate(BaseModel):
    """Tags / is_default only — file content is immutable after upload."""

    tags: list[str] | None = None
    is_default: bool | None = None
