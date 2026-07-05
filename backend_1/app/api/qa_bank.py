"""Q&A bank CRUD endpoints."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.qa_bank import QABankEntry
from app.schemas.qa_bank import (
    QABankEntryCreate,
    QABankEntryResponse,
    QABankEntryUpdate,
)

router = APIRouter(prefix="/qa-bank", tags=["qa-bank"])


@router.get("", response_model=list[QABankEntryResponse])
async def list_entries(
    unanswered_first: bool = True,
    db: AsyncSession = Depends(get_db),
):
    query = select(QABankEntry)
    if unanswered_first:
        query = query.order_by(
            (QABankEntry.answer == None).desc(),  # noqa: E711
            QABankEntry.times_used.desc(),
        )
    else:
        query = query.order_by(QABankEntry.times_used.desc())
    result = await db.execute(query)
    return [QABankEntryResponse.model_validate(e) for e in result.scalars().all()]


@router.post("", response_model=QABankEntryResponse, status_code=201)
async def create_entry(
    entry: QABankEntryCreate,
    db: AsyncSession = Depends(get_db),
):
    new_entry = QABankEntry(
        question_pattern=entry.question_pattern,
        canonical_question=entry.canonical_question,
        answer=entry.answer,
        answer_type=entry.answer_type,
    )
    db.add(new_entry)
    await db.commit()
    await db.refresh(new_entry)
    return QABankEntryResponse.model_validate(new_entry)


@router.patch("/{entry_id}", response_model=QABankEntryResponse)
async def update_entry(
    entry_id: UUID,
    update: QABankEntryUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QABankEntry).where(QABankEntry.id == entry_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    if update.answer is not None:
        entry.answer = update.answer
    if update.canonical_question is not None:
        entry.canonical_question = update.canonical_question
    if update.answer_type is not None:
        entry.answer_type = update.answer_type
    await db.commit()
    await db.refresh(entry)
    return QABankEntryResponse.model_validate(entry)


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(entry_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(QABankEntry).where(QABankEntry.id == entry_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.delete(entry)
    await db.commit()
