"""Pydantic schemas for company API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CompanyResponse(BaseModel):
    id: UUID
    name: str
    name_slug: str
    website: str | None = None
    funding_amount: float | None = None
    funding_stage: str = "unknown"
    funding_date: datetime | None = None
    source: str
    source_url: str | None = None
    founder_name: str | None = None
    founder_twitter: str | None = None
    founder_linkedin: str | None = None
    team_size: int | None = None
    description: str | None = None
    hiring_intent_score: int = 0
    hiring_signals: list = Field(default_factory=list)
    likely_roles: list = Field(default_factory=list)
    company_summary: str | None = None
    category: str = "startup"
    status: str = "new"
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompanyListResponse(BaseModel):
    companies: list[CompanyResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class CompanyStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(new|contacted|interviewing|pass)$")


class PipelineStats(BaseModel):
    total_companies: int = 0
    new_today: int = 0
    high_intent: int = 0
    contacted: int = 0
    ngo_count: int = 0
