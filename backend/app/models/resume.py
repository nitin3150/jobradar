import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

MAX_TAGS_PER_RESUME = 32
MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # ARRAY(String) — Postgres native via asyncpg. Empty list = stored anywhere.
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, server_default="{}"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", index=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Cached plain-text extraction of the resume file. Filled at upload time
    # (see app/api/resumes.py). Nullable: extraction is best-effort.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
