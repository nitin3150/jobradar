"""Q&A Bank router — answers used to auto-fill job application forms.

In-memory seeded store. The React ``QABank`` page renders each entry
with its question text, the answer input cell, and a usage counter so
operators can see which answers get re-used the most.

Frontend wire shape (see ``frontend/src/pages/QABank.jsx``):

* ``GET /api/qa-bank`` → ``[entry, ...]`` (a flat list — the React
  component ``entries?.map(...)`` reads ``r.data`` as a list directly,
  not wrapped in an envelope. The hook's :func:`useQABank` therefore
  unwraps the ``'data'`` field; the server returns a plain JSON array.)
* ``POST /api/qa-bank`` body
  ``{"question_pattern": "...", "canonical_question": "...", "answer": "..."}``.
  ``answer`` may be omitted / null and backfilled later. ``answer_type``
  is derived server-side from the answer length so the React table
  can render a "Type" badge without a separate column on the form.
* ``PATCH /api/qa-bank/{id}`` body partial — the most common case in
  the UI is just ``{"answer": "..."}`` from the inline editor.
* ``DELETE /api/qa-bank/{id}`` removes by id; the response body is
  the deleted entry (consumed by :func:`useDeleteQAEntry` if the
  mutation onSuccess ever reads it).
"""
from __future__ import annotations

import copy
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field


router = APIRouter()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
AnswerType = Literal["short_text", "long_text"]


# Threshold distinguishing a one-line answer (``short_text``: name,
# years of experience) from a paragraph (``long_text``: motivation,
# background). Mirrors what the React ``EntryRow`` would render
# visually — short answers can fit in a single-line input, paragraphs
# want a textarea.
SHORT_TEXT_LIMIT = 120


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class QAEntry(BaseModel):
    id: str
    question_pattern: str  # lowercase dedupe-key, matches the React hook's input
    canonical_question: str  # user-facing display text
    answer: str | None = None
    answer_type: AnswerType
    times_used: int = Field(default=0, ge=0)


class QAEntryCreate(BaseModel):
    question_pattern: str = Field(min_length=1, max_length=200)
    canonical_question: str = Field(min_length=1, max_length=200)
    answer: str | None = Field(default=None, max_length=2000)


class QAEntryPatch(BaseModel):
    question_pattern: str | None = Field(default=None, min_length=1, max_length=200)
    canonical_question: str | None = Field(default=None, min_length=1, max_length=200)
    answer: str | None = Field(default=None, max_length=2000)


class QAListResponse(BaseModel):
    entries: list[QAEntry]
    total: int


# ---------------------------------------------------------------------------
# Seeded store — 6 demo entries, 3 with no answer (highlighted orange in
# the React table) so the "fill the missing ones" UX has meaning even
# before the user adds anything.
# ---------------------------------------------------------------------------
_SEED_RECORDS: list[dict] = [
    {
        "id": "q1",
        "question_pattern": "years of experience",
        "canonical_question": "Years of experience",
        "answer": "5 years of professional software engineering, 3 focused on AI/ML systems.",
        "answer_type": "short_text",
        "times_used": 14,
    },
    {
        "id": "q2",
        "question_pattern": "why this company",
        "canonical_question": "Why are you interested in this company?",
        "answer": None,  # missing → orange row in the React UI
        "answer_type": "short_text",
        "times_used": 0,
    },
    {
        "id": "q3",
        "question_pattern": "relevant project",
        "canonical_question": "Describe a relevant project",
        "answer": (
            "I rebuilt our inference layer at Replicate to support model hot-swapping "
            "with zero request drops — published a write-up and open-sourced the "
            "blue/green router component."
        ),
        "answer_type": "long_text",
        "times_used": 7,
    },
    {
        "id": "q4",
        "question_pattern": "notice period",
        "canonical_question": "What is your notice period?",
        "answer": None,
        "answer_type": "short_text",
        "times_used": 0,
    },
    {
        "id": "q5",
        "question_pattern": "work authorization",
        "canonical_question": "Are you authorized to work in the role's location?",
        "answer": None,
        "answer_type": "short_text",
        "times_used": 0,
    },
    {
        "id": "q6",
        "question_pattern": "salary expectation",
        "canonical_question": "Salary expectation",
        "answer": (
            "Open depending on equity and remote-flexibility. I'd rather not anchor — "
            "happy to walk through comp ranges once we both have signal that the role "
            "is a fit."
        ),
        "answer_type": "long_text",
        "times_used": 11,
    },
]


_QA_DB: dict[str, dict] = {}


def _seed() -> None:
    _QA_DB.clear()
    for rec in _SEED_RECORDS:
        _QA_DB[rec["id"]] = copy.deepcopy(rec)


_seed()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _derive_answer_type(answer: str | None) -> AnswerType:
    """``short_text`` for absent / short answers; ``long_text`` otherwise."""
    if answer is None or len(answer) <= SHORT_TEXT_LIMIT:
        return "short_text"
    return "long_text"


def _clean_answer(raw: str | None) -> str | None:
    """Normalize ``answer`` to ``None`` when blank, else a trimmed string.

    Shared between POST + PATCH so an empty / whitespace-only input is
    treated identically on both code paths. The React UI gates its
    orange "no answer" highlight on ``answer || '⚠'`` ; if POST/PATCH
    diverged here, the badge would silently flip when a future caller
    sends ``""`` or ``"   "`` from outside the QABank form.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned if cleaned else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=QAListResponse)
def list_qa_entries() -> QAListResponse:
    """Return every entry, most-used first so the React table surfaces
    high-frequency questions at the top."""
    records = sorted(
        _QA_DB.values(),
        key=lambda e: (-e["times_used"], e["canonical_question"]),
    )
    return QAListResponse(
        entries=[QAEntry(**e) for e in records],
        total=len(records),
    )


@router.post("", response_model=QAEntry, status_code=201)
def create_qa_entry(payload: QAEntryCreate) -> QAEntry:
    """Create a new entry; ``times_used`` starts at 0 and ``answer_type``
    is derived from the supplied ``answer`` so the React table's "Type"
    column has the right value immediately."""
    entry_id = uuid4().hex[:8]
    answer = _clean_answer(payload.answer)
    record = {
        "id": entry_id,
        "question_pattern": payload.question_pattern.strip().lower(),
        "canonical_question": payload.canonical_question.strip(),
        "answer": answer,
        "answer_type": _derive_answer_type(answer),
        "times_used": 0,
    }
    _QA_DB[entry_id] = record
    return QAEntry(**record)


@router.patch("/{entry_id}", response_model=QAEntry)
def patch_qa_entry(
    payload: QAEntryPatch,
    entry_id: str = Path(min_length=1, max_length=64),
) -> QAEntry:
    """Partial update. When the ``answer`` field changes, ``answer_type``
    is re-derived so the React table's badge stays consistent."""
    rec = _QA_DB.get(entry_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"qa entry {entry_id!r} not found")

    if payload.question_pattern is not None:
        rec["question_pattern"] = payload.question_pattern.strip().lower()

    if payload.canonical_question is not None:
        rec["canonical_question"] = payload.canonical_question.strip()

    if payload.answer is not None:
        rec["answer"] = _clean_answer(payload.answer)
        rec["answer_type"] = _derive_answer_type(rec["answer"])

    return QAEntry(**rec)


@router.delete("/{entry_id}", response_model=QAEntry)
def delete_qa_entry(entry_id: str = Path(min_length=1, max_length=64)) -> QAEntry:
    """Remove the entry and return the deleted record so any future
    onSuccess handlers (cache reconciliation, undo toast) have the
    full record handy."""
    rec = _QA_DB.pop(entry_id, None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"qa entry {entry_id!r} not found")
    return QAEntry(**rec)
