from datetime import datetime

from pydantic import BaseModel, Field


class PreferencesResponse(BaseModel):
    target_roles: list[str]
    review_window_hours: float
    job_fit_threshold: float
    send_followup_emails: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class PreferencesUpdate(BaseModel):
    """All fields optional. Only fields actually present in the payload are applied."""

    target_roles: list[str] | None = Field(default=None)
    review_window_hours: float | None = Field(default=None, ge=0.5, le=48)
    job_fit_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    send_followup_emails: bool | None = Field(default=None)
