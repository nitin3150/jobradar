import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnswerType(str, enum.Enum):
    TEXT = "text"
    BOOLEAN = "boolean"
    NUMBER = "number"
    SELECT = "select"


class QABankEntry(Base):
    __tablename__ = "qa_bank_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    question_pattern: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    canonical_question: Mapped[str] = mapped_column(String(512), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    answer_type: Mapped[str] = mapped_column(
        String(32), default=AnswerType.TEXT.value
    )
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
