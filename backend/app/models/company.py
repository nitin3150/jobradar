import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FundingStage(str, enum.Enum):
    PRE_SEED = "pre-seed"
    SEED = "seed"
    SERIES_A = "series-a"
    SERIES_B = "series-b"
    SERIES_C = "series-c"
    UNKNOWN = "unknown"


class CompanyStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    INTERVIEWING = "interviewing"
    PASS = "pass"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    name_slug: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True, index=True
    )
    website: Mapped[str | None] = mapped_column(String(1024))
    funding_amount: Mapped[float | None] = mapped_column(Float)
    funding_stage: Mapped[str] = mapped_column(
        String(32), default=FundingStage.UNKNOWN.value
    )
    funding_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    founder_name: Mapped[str | None] = mapped_column(String(256))
    founder_twitter: Mapped[str | None] = mapped_column(String(256))
    founder_linkedin: Mapped[str | None] = mapped_column(String(512))
    team_size: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    hiring_intent_score: Mapped[int] = mapped_column(Integer, default=0)
    hiring_signals: Mapped[dict | None] = mapped_column(JSONB, default=list)
    likely_roles: Mapped[dict | None] = mapped_column(JSONB, default=list)
    company_summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(
        String(32), default="startup", index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=CompanyStatus.NEW.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    outreach_messages = relationship(
        "OutreachMessage", back_populates="company", cascade="all, delete-orphan"
    )
    jobs = relationship("Job", back_populates="company", cascade="all, delete-orphan")
