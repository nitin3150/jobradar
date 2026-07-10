"""Shared types for :mod:`apply_worker` modules.

Lightweight :class:`dataclasses.dataclass` rather than
:class:`pydantic.BaseModel` because these objects are short-lived
scratch types that never hit the wire. Each module also accepts
plain ``dict`` input (see module docstrings) — dataclasses are
offered for the ``from_data()`` constructor convenience + IDE
type-check friendliness; callers can mix-and-match dict/dataclass
without conversion overhead.

Wire shape mirrors what the Supabase tables already return per the
JSON serialiser on :class:`db.models.QABankEntry` /
:class:`db.models.Resume` / :class:`db.models.Job`. So a real
worker fetches from Postgres and hands the
``.model_dump(mode="json")`` dict directly to the module — no
field-by-field mapping required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Match-source enum + dataclasses
# ---------------------------------------------------------------------------

# Disambiguates where a ``match_questions`` row came from. The
# operator-facing UI surfaces this as a small badge (e.g. "matched by
# rapidfuzz" vs "matched by LLM") so they can spot-check whether the
# matcher is being decisive enough on local signals.
MATCH_SOURCE_RAPIDFUZZ = "rapidfuzz"
MATCH_SOURCE_LLM = "llm"
MATCH_SOURCE_NONE = "none"


@dataclass(slots=True)
class QABankRecord:
    """Minimal Q&A bank entry — matches the Postgres ``qa_bank_entries`` row shape.

    ``id`` is opaque (Supabase returns ``uuid.uuid4``-derived strings
    by default). ``question_pattern`` is the lowercase dedupe key the
    matcher fuzzy-matches against form field labels. ``answer``
    stays as ``None`` if the operator hasn't filled it in yet — the
    matcher returns a hit but the worker must surface the unfilled
    state up the call chain so the apply step can short-circuit OR
    save a new entry on abort.
    """

    id: str
    question_pattern: str
    canonical_question: str
    answer: str | None = None
    answer_type: str = "short_text"
    times_used: int = 0

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "QABankRecord":
        return cls(
            id=str(data["id"]),
            question_pattern=str(data["question_pattern"]),
            canonical_question=str(data["canonical_question"]),
            answer=data.get("answer"),
            answer_type=data.get("answer_type", "short_text"),
            times_used=int(data.get("times_used") or 0),
        )


@dataclass(slots=True)
class FormFieldRecord:
    """Single form field as extracted by the future Playwright ``form_filler``.

    ``label`` is the human-readable label rendered next to the input
    (e.g. ``"What is your earliest available start date?"``).
    ``field_type`` is one of ``"text"``, ``"textarea"``, ``"select"``,
    ``"radio"``, ``"checkbox"``, ``"file"`` — used by the worker to
    skip non-text fields (``file`` uploads, ``checkbox`` yes/no)
    from Q&A matching (those don't need Q&A bank entries).

    ``select_options`` is populated only for ``field_type ==
    "select"`` / ``"radio"`` — the list of valid choice values. The
    matcher uses these to match against the QABank entry's answer
    if the entry is a multi-choice question (e.g. "Willing to
    relocate? yes / no").
    """

    label: str
    field_type: str = "text"
    select_options: list[str] = field(default_factory=list)
    # Stable per-form-field id (form filler's ``name=`` attribute or
    # a synthetic ``f1, f2, ...``). The LLM batch prompt uses this
    # so the response can be keyed back to a specific field without
    # # relying on label-string ambiguity.
    field_id: str = ""

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "FormFieldRecord":
        return cls(
            label=str(data["label"]),
            field_type=data.get("field_type", "text"),
            select_options=list(data.get("select_options") or []),
            field_id=str(data.get("field_id", "")),
        )


@dataclass(slots=True)
class MatchResult:
    """Per-form-field match outcome returned by :func:`apply_worker.qa_matcher.match_questions`."""

    label: str
    field_id: str
    entry_id: str | None  # ``None`` when ``source == MATCH_SOURCE_NONE``
    confidence: float  # 0.0–1.0 (rapidfuzz/100 normalised) OR 0.0–1.0 LLM score
    source: str  # "rapidfuzz" | "llm" | "none"
    reasoning: str = ""  # only populated when source == "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "field_id": self.field_id,
            "entry_id": self.entry_id,
            "confidence": self.confidence,
            "source": self.source,
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# Resume + Job — input shapes for :func:`apply_worker.resume_picker.pick_resume`.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResumeRecord:
    """Minimal resume metadata — matches the Postgres ``resumes`` row shape.

    Only metadata is read here. The actual file bytes live in
    Supabase Storage at ``storage_path`` and are downloaded by the
    future ``form_filler``. The picker never touches bytes — it
    matches purely on tags + ``is_default`` policy.

    ``uploaded_at`` is kept so the tiebreaker for matches with equal
    overlap is "newest resume wins". ISO-8601 string for ergonomic
    comparison (Python sorts datetime-aware strings correctly when
    all entries share a timezone suffix — we normalise to ``Z`` /
    ``+00:00`` on write).
    """

    id: str
    name: str
    tags: list[str] = field(default_factory=list)
    is_default: bool = False
    uploaded_at: str = ""
    storage_path: str = ""

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ResumeRecord":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            tags=list(data.get("tags") or []),
            is_default=bool(data.get("is_default", False)),
            uploaded_at=str(data.get("uploaded_at") or ""),
            storage_path=str(data.get("storage_path") or ""),
        )


@dataclass(slots=True)
class JobRecord:
    """Minimal job fields needed by the picker — title + description.

    The :class:`Job` SQLAlchemy model has many columns; this dataclass
    keeps only what ``derive_role_family_tags`` reads. Callers pass
    the full ``Job.model_dump(mode="json")`` dict — extra keys are
    ignored.
    """

    title: str = ""
    description: str = ""
    ats_type: str = ""
    company_name: str = ""

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "JobRecord":
        return cls(
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            ats_type=str(data.get("ats_type") or ""),
            company_name=str(data.get("company_name") or ""),
        )


__all__ = [
    "MATCH_SOURCE_RAPIDFUZZ",
    "MATCH_SOURCE_LLM",
    "MATCH_SOURCE_NONE",
    "QABankRecord",
    "FormFieldRecord",
    "MatchResult",
    "ResumeRecord",
    "JobRecord",
]
