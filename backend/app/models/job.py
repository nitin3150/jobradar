import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ATSType(str, enum.Enum):
    ASHBY = "ashby"
    LEVER = "lever"
    GREENHOUSE = "greenhouse"


class JobStatus(str, enum.Enum):
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FLAGGED = "flagged"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ats_type: Mapped[str] = mapped_column(String(32), nullable=False)
    jd_text: Mapped[str | None] = mapped_column(Text)
    ai_fit_score: Mapped[float | None] = mapped_column(Float)
    ai_fit_reasoning: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.IN_REVIEW.value, index=True
    )
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    review_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    application = relationship(
        "Application", back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    company = relationship("Company", back_populates="jobs")
