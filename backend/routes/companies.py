"""Companies router — saved opportunities with lifecycle status.

In-memory CRUD-style store so ``curl localhost:8000/api/companies``
returns JSON immediately. The status field tracks the user's CRM-style
pipeline on the company itself (``saved`` → ``interested`` →
``outreach_sent`` → ``engaged``), distinct from the post-application
pipeline in ``/api/applications/*`` (``submitted`` → ``interview`` →
``rejected`` / ``offer`` / ``ghosted``).

Closes the wire gap so the React ``CompanyFeed`` + ``CompanyCard`` +
the ``useUpdateCompanyStatus`` mutation can resolve against the new
``/api/*`` prefix. Swap ``_COMPANIES_DB`` for a DB-backed store once
the persistence layer lands — the routes' public shape is stable.
"""
import copy
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field


router = APIRouter()


# --------------------------------------------------------------------------
# Enums (Pydantic ``Literal`` matches the Vite badge color tables on the
# React side; expanding the enum requires adding the new color + filter
# key on the frontend too).
# --------------------------------------------------------------------------
CompanyStatus = Literal[
    "saved",
    "interested",
    "dismissed",
    "outreach_sent",
    "engaged",
]
CategoryType = Literal["boards", "funding", "ngos", "oss", "remote"]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Company(BaseModel):
    id: str
    title: str
    organization: str

    # Wire fields CompanyCard reads
    url: str | None = None
    category: CategoryType
    score: float = Field(ge=0.0, le=1.0)
    source: str
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    published: str  # ISO 8601
    location: str | None = None
    primary_language: str | None = None
    difficulty: str | None = None
    stars: int | None = None

    # Wire fields OutreachPanel reads (opened via generate_outreach button)
    company_summary: str | None = None
    hiring_signals: list[str] = Field(default_factory=list)

    # Lifecycle
    status: CompanyStatus = "saved"
    created_at: str
    updated_at: str


class CompanyPatchStatus(BaseModel):
    status: CompanyStatus


class CompanyListResponse(BaseModel):
    companies: list[Company]
    total: int
    count: int


class CompanyStatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    by_source: dict[str, int]


# --------------------------------------------------------------------------
# Seeded store — the canonical seed list is immutable; the runtime dict is
# rebuilt from it via ``_seed()`` so tests can reset between cases without
# import-time gymnastics (``importlib.reload`` would invalidate the
# router reference registered with ``app.include_router``).
# --------------------------------------------------------------------------
_NOW_SEED = "2026-01-10T00:00:00Z"

_SEED_RECORDS: list[dict] = [
    {
        "id": "c_1",
        "title": "Senior Frontend Engineer",
        "organization": "Vercel",
        "url": "https://vercel.com/careers",
        "category": "boards",
        "score": 0.95,
        "source": "ashby",
        "tags": ["react", "typescript", "nextjs"],
        "description": "Build the edge-first user experience layer.",
        "published": "2026-01-08T10:00:00Z",
        "location": "Remote",
        "company_summary": "Vercel runs the frontend cloud — Next.js, edge functions, and AI tooling.",
        "hiring_signals": ["Recently closed Series D", "Expanding the DX team"],
        "status": "interested",
        "created_at": _NOW_SEED,
        "updated_at": _NOW_SEED,
    },
    {
        "id": "c_2",
        "title": "Maintainer help wanted: pydantic",
        "organization": "pydantic",
        "url": "https://github.com/pydantic/pydantic",
        "category": "oss",
        "score": 0.88,
        "source": "github_issues",
        "tags": ["python", "open source"],
        "description": "Good first issues open in the pydantic repo.",
        "published": "2026-01-09T12:00:00Z",
        "primary_language": "Python",
        "difficulty": "medium",
        "stars": 21000,
        "company_summary": "Pydantic is the data validation library under FastAPI, LangChain, and most modern Python stacks.",
        "hiring_signals": ["Maintainer open call for a co-maintainer (community post)"],
        "status": "saved",
        "created_at": _NOW_SEED,
        "updated_at": _NOW_SEED,
    },
    {
        "id": "c_3",
        "title": "Async Python Developer",
        "organization": "Doist",
        "url": "https://doist.com/careers",
        "category": "remote",
        "score": 0.81,
        "source": "remotive",
        "tags": ["python", "asyncio", "postgres"],
        "description": "Remote-first async backend role at the company behind Todoist.",
        "published": "2026-01-07T09:00:00Z",
        "location": "Worldwide",
        "company_summary": "Doist is a 30+ year-old async-first remote company powering Todoist and Twist.",
        "hiring_signals": ["Async-first culture blog series in Q4"],
        "status": "engaged",
        "created_at": _NOW_SEED,
        "updated_at": _NOW_SEED,
    },
    {
        "id": "c_4",
        "title": "AI Safety Researcher",
        "organization": "Anthropic",
        "url": "https://www.anthropic.com/careers",
        "category": "funding",
        "score": 0.79,
        "source": "producthunt",
        "tags": ["ai safety", "research"],
        "description": "Interpretability research opening surfaced via the funding-news feed.",
        "published": "2026-01-06T15:00:00Z",
        "location": "San Francisco",
        "company_summary": "Anthropic is the public-benefit corp behind Claude, focused on safe frontier models.",
        "hiring_signals": ["Funding round covered — likely headcount growth"],
        "status": "outreach_sent",
        "created_at": _NOW_SEED,
        "updated_at": _NOW_SEED,
    },
    {
        "id": "c_5",
        "title": "Open Source Engineer — Climate Tech",
        "organization": "Open Climate Fix",
        "url": "https://openclimatefix.org/jobs",
        "category": "ngos",
        "score": 0.74,
        "source": "idealist",
        "tags": ["climate", "open source", "ml"],
        "description": "Maintain and extend the open-source nowcasting pipeline.",
        "published": "2026-01-05T18:00:00Z",
        "location": "Remote (UK)",
        "company_summary": "Open Climate Fix publishes open-source ML for solar and electricity-grid nowcasting.",
        "hiring_signals": ["Active grant cycle Q1", "Two new repos opened this week"],
        "status": "saved",
        "created_at": _NOW_SEED,
        "updated_at": _NOW_SEED,
    },
    {
        "id": "c_6",
        "title": "Distributed Systems Engineer",
        "organization": "Cloudflare",
        "url": "https://cloudflare.com/careers",
        "category": "boards",
        "score": 0.69,
        "source": "greenhouse",
        "tags": ["rust", "distributed systems", "edge"],
        "description": "Edge-runtime engineer role on the Workers team.",
        "published": "2026-01-04T12:00:00Z",
        "location": "Remote / Lisbon",
        "company_summary": "Cloudflare runs ~20% of the web; edge runtime + Workers are core bets.",
        "hiring_signals": [],
        "status": "dismissed",
        "created_at": _NOW_SEED,
        "updated_at": _NOW_SEED,
    },
]


_COMPANIES_DB: dict[str, dict] = {}


def _seed() -> None:
    """Reset ``_COMPANIES_DB`` to a deep copy of the canonical seed records.

    Each record is deep-copied so a PATCH mutation cannot bleed back into
    ``_SEED_RECORDS`` between tests.
    """
    _COMPANIES_DB.clear()
    for rec in _SEED_RECORDS:
        _COMPANIES_DB[rec["id"]] = copy.deepcopy(rec)


# Initialize at import time so the routes have live data immediately.
_seed()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Routes — ``GET /stats`` is declared BEFORE ``GET /{company_id}`` so the
# literal path takes precedence over the dynamic one even though FastAPI
# matches ``/stats`` and ``/c_1`` against the same pattern otherwise.
# Order matters: reordering these two declarations would silently shadow
# ``/stats`` with the ``{company_id}`` handler.
# --------------------------------------------------------------------------
@router.get("/stats", response_model=CompanyStatsResponse)
def get_companies_stats() -> CompanyStatsResponse:
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for c in _COMPANIES_DB.values():
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        by_category[c["category"]] = by_category.get(c["category"], 0) + 1
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    return CompanyStatsResponse(
        total=len(_COMPANIES_DB),
        by_status=by_status,
        by_category=by_category,
        by_source=by_source,
    )


@router.get("", response_model=CompanyListResponse)
def list_companies(
    category: str | None = Query(default=None),
    source: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CompanyListResponse:
    # Filter in memory; ``total`` reflects total matched post-filter so the
    # response envelope is useful for "show 6 of 47" pagination UI.
    matches: list[dict] = list(_COMPANIES_DB.values())
    if category:
        matches = [c for c in matches if c["category"] == category]
    if source:
        matches = [c for c in matches if c["source"] == source]
    if status_filter:
        matches = [c for c in matches if c["status"] == status_filter]
    if search:
        needle = search.lower()
        matches = [
            c
            for c in matches
            if needle in (c.get("title") or "").lower()
            or needle in (c.get("organization") or "").lower()
            or needle in (c.get("description") or "").lower()
            or any(needle in (t or "").lower() for t in c.get("tags") or [])
        ]
    total = len(matches)
    page = matches[offset : offset + limit]
    return CompanyListResponse(
        companies=[Company(**c) for c in page],
        total=total,
        count=len(page),
    )


@router.get("/{company_id}", response_model=Company)
def get_company(company_id: str = Path(min_length=1, max_length=120)) -> Company:
    rec = _COMPANIES_DB.get(company_id)
    if rec is None:
        raise HTTPException(
            status_code=404, detail=f"company {company_id!r} not found"
        )
    return Company(**rec)


@router.patch("/{company_id}/status", response_model=Company)
def patch_company_status(
    payload: CompanyPatchStatus,
    company_id: str = Path(min_length=1, max_length=120),
) -> Company:
    rec = _COMPANIES_DB.get(company_id)
    if rec is None:
        raise HTTPException(
            status_code=404, detail=f"company {company_id!r} not found"
        )
    rec["status"] = payload.status
    rec["updated_at"] = _now_iso()
    return Company(**rec)
