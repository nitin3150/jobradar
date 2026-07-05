"""Pydantic schemas for outreach API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    name: str = ""
    role: str = ""
    skills: list[str] = Field(default_factory=list)
    background: str = ""


class OutreachRequest(BaseModel):
    company_id: UUID
    type: str = Field(..., pattern="^(email|twitter_dm|linkedin)$")
    user_context: UserContext = Field(default_factory=UserContext)


class OutreachResponse(BaseModel):
    id: UUID
    company_id: UUID
    type: str
    content: str
    generated_at: datetime

    model_config = {"from_attributes": True}
