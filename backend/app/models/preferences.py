from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# Mirrors the defaults currently in frontend/src/hooks/usePreferences.js so a
# fresh GET after migrate returns the same shape the Preferences modal paints.
class Preferences(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    target_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, server_default="{}"
    )
    review_window_hours: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="2.0"
    )
    job_fit_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0.6"
    )
    send_followup_emails: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Singleton — only ever id=1.
    SINGLETON_ID = 1


# Defaults used by both the migration (server_default) and the GET-on-empty
# seed path so they stay in sync.
DEFAULT_TARGET_ROLES = [
    "AI Engineer",
    "Machine Learning Engineer",
    "LLM Engineer",
    "Software Engineer",
]
DEFAULT_REVIEW_WINDOW_HOURS = 2.0
DEFAULT_JOB_FIT_THRESHOLD = 0.6
DEFAULT_SEND_FOLLOWUP_EMAILS = True
