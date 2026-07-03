from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class QABankEntryResponse(BaseModel):
    id: UUID
    question_pattern: str
    canonical_question: str
    answer: str | None = None
    answer_type: str
    times_used: int
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class QABankEntryCreate(BaseModel):
    question_pattern: str
    canonical_question: str
    answer: str | None = None
    answer_type: str = "text"


class QABankEntryUpdate(BaseModel):
    answer: str | None = None
    canonical_question: str | None = None
    answer_type: str | None = None
