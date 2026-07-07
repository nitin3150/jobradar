"""Outreach message generation endpoint.

Thin wrapper over the QA-bank + resume-selection logic. The QA-bank and
Resumes routers are separate scope (not yet implemented), so we inline
small seeded copies of their records here and select from them based on
the request's ``type``, ``company_id``, and ``user_context.skills``.

Storage is an in-memory dict keyed by ``company_id``; messages do NOT
survive process restarts. Swap for a real DB-backed store once a
persistence layer lands.

Route ordering note
-------------------
``POST /generate`` is declared BEFORE ``GET /{company_id}``. POST requests
to ``/generate`` correctly hit the static route. ``GET /generate`` would
be silently intercepted as ``fetch_outreach_messages(company_id="generate")``
returning ``[]`` — benign in practice because the React side's
``companyId`` is always a UUID hash, never the literal string ``"generate"``,
but document the behaviour so a future maintainer doesn't reshuffle the
routers into a state where ``GET /generate`` accidentally matches first.
"""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field


router = APIRouter()


# --------------------------------------------------------------------------
# Seeded QA-bank + Resumes — replace with cross-router HTTP calls once the
# QA-bank and Resumes routers land. Mirrors the shapes the frontend expects
# (see ``frontend/src/pages/QABank.jsx`` and ``ResumesModal.jsx``).
# --------------------------------------------------------------------------
_SEED_RESUMES: list[dict] = [
    {
        "id": "r1",
        "name": "ml-engineer.pdf",
        "tags": {"ml", "python", "pytorch"},
        "is_default": True,
        "uploaded_at": "2026-01-04T00:00:00Z",
    },
    {
        "id": "r2",
        "name": "backend-api.pdf",
        "tags": {"backend", "fastapi", "python"},
        "is_default": False,
        "uploaded_at": "2026-01-05T00:00:00Z",
    },
    {
        "id": "r3",
        "name": "frontend-react.pdf",
        "tags": {"frontend", "react", "typescript"},
        "is_default": False,
        "uploaded_at": "2026-01-02T00:00:00Z",
    },
]

_SEED_QA_BANK: list[dict] = [
    {
        "id": "q1",
        "category": "ml",
        "snippet": "I've published a couple of papers on efficient training and shipped agent training pipelines at scale.",
    },
    {
        "id": "q2",
        "category": "backend",
        "snippet": "I've shipped FastAPI services handling high-throughput traffic and care a lot about measurable latency.",
    },
    {
        "id": "q3",
        "category": "frontend",
        "snippet": "I've built React/TypeScript apps with strong component contracts and end-to-end tests.",
    },
]


# --------------------------------------------------------------------------
# Request / Response models
# --------------------------------------------------------------------------
class UserContext(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    skills: list[str] = Field(default_factory=list, max_length=64)
    background: str | None = Field(default=None, max_length=2000)


class GenerateOutreachRequest(BaseModel):
    company_id: str = Field(min_length=1, max_length=120)
    type: Literal["email", "twitter_dm", "linkedin"]
    user_context: UserContext


class OutreachMessage(BaseModel):
    id: str
    company_id: str
    type: Literal["email", "twitter_dm", "linkedin"]
    content: str
    created_at: str
    resume_picked_id: str
    resume_picked_name: str
    qa_snippet_id: str
    qa_snippet: str


# --------------------------------------------------------------------------
# Storage — dict keyed by ``company_id``. The frontend's
# ``fetchOutreachMessages(companyId)`` query is an O(1) fetch against this.
# --------------------------------------------------------------------------
_MESSAGES_DB: dict[str, list[dict]] = {}


# --------------------------------------------------------------------------
# Selection helpers
# --------------------------------------------------------------------------
def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _skill_set(skills: list[str]) -> set[str]:
    return {s.lower().strip() for s in skills if s and s.strip()}


def _pick_resume(skills: list[str]) -> dict:
    skill_set = _skill_set(skills)

    def score(r: dict) -> tuple[float, int, str]:
        # Highest Jaccard first (``-`` flips ascending sort into descending);
        # ``is_default`` True wins ties; oldest upload keeps multi-tie deterministic.
        return (
            -_jaccard(skill_set, {t.lower() for t in r["tags"]}),
            -1 if r["is_default"] else 0,
            r["uploaded_at"],
        )

    return sorted(_SEED_RESUMES, key=score)[0]


def _pick_qa(skills: list[str]) -> dict:
    skill_set = _skill_set(skills)
    matches = [q for q in _SEED_QA_BANK if q["category"].lower() in skill_set]
    if not matches:
        return _SEED_QA_BANK[0]
    # Deterministic tie-break by id ascending so seed order doesn't pollute tests.
    return sorted(matches, key=lambda q: q["id"])[0]


def _cap(text: str, max_chars: int) -> str:
    """Hard-cap text at ``max_chars`` and emit a proper ('…') suffix on overflow.

    The brute ``text[:max_chars]`` slice would cut a character mid-word (e.g.
    ``"engineer"`` → ``"enginee"``), producing visibly broken copy that an
    operator would hand-paste into a Twitter DM or LinkedIn request.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "\u2026"


def _render(
    type_: str,
    company_id: str,
    ctx: dict[str, object],
    resume: dict,
    qa: dict,
) -> str:
    skills_line = ", ".join(ctx["skills"]) if ctx["skills"] else "building useful software"
    name = (ctx.get("name") or "").strip()
    role = (ctx.get("role") or "").strip()
    background = (ctx.get("background") or "").strip()
    signature = f"{name} — {role}" if name else role

    if type_ == "email":
        return (
            f"Hi {company_id} team,\n\n"
            f"I noticed the work you're shipping and would love to contribute. {qa['snippet']} "
            f"{background + ' ' if background else ''}"
            f"My tooling focus is {skills_line}.\n\n"
            f"I've attached the matching resume (id: {resume['id']}, {resume['name']}) — "
            f"would a 20-minute introductory call next week work for you?\n\n"
            f"Thanks,\n{signature}"
        )
    if type_ == "twitter_dm":
        msg = (
            f"Hey {company_id} team — I'm {name} ({role}). "
            f"Just applied via your jobs email and dropped {resume['name']} in. "
            f"Would love to chat! {qa['snippet'][:80]}…"
        )
        return msg[:280]
    # linkedin
    msg = (
        f"Hi! I'm {name}, focusing on {skills_line}. "
        f"{qa['snippet'][:140]} I'd love to bring that energy to {company_id} as a {role}. "
        f"(Resume: {resume['name']})"
    )
    return msg[:300]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.post("/generate", response_model=OutreachMessage)
def generate_outreach(payload: GenerateOutreachRequest) -> OutreachMessage:
    user_ctx = payload.user_context.model_dump()
    resume = _pick_resume(user_ctx["skills"])
    qa = _pick_qa(user_ctx["skills"])
    content = _render(payload.type, payload.company_id, user_ctx, resume, qa)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    msg = OutreachMessage(
        id=uuid4().hex,
        company_id=payload.company_id,
        type=payload.type,
        content=content,
        created_at=created_at,
        resume_picked_id=resume["id"],
        resume_picked_name=resume["name"],
        qa_snippet_id=qa["id"],
        qa_snippet=qa["snippet"],
    )
    _MESSAGES_DB.setdefault(payload.company_id, []).append(msg.model_dump())
    return msg


@router.get("/{company_id}", response_model=list[OutreachMessage])
def fetch_outreach_messages(
    company_id: str = Path(min_length=1, max_length=120),
) -> list[OutreachMessage]:
    raw = _MESSAGES_DB.get(company_id, [])
    # Newest first — created_at is ISO 8601, lexicographic ordering matches
    # chronological order, so a plain string sort suffices.
    sorted_raw = sorted(raw, key=lambda m: m["created_at"], reverse=True)
    return [OutreachMessage(**r) for r in sorted_raw]
