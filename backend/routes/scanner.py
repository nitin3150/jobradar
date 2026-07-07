"""Domain-organized scanner routes.

Each tab in the frontend maps to one of the five ``POST /scan/<domain>``
endpoints here. Every domain accepts ``delta_hours`` so callers can ask
for items posted in the last N hours, plus ``limit`` and ``sources``.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from pipeline.nodes.jobs_boards.runner import UnknownBoardError, run_all as run_jobs_boards
from pipeline.nodes.funding.runner import scan_funding
from pipeline.nodes.ngos.runner import scan_ngos
from pipeline.nodes.oss.runner import scan_oss
from pipeline.nodes.remote.runner import scan_remote

router = APIRouter()


def _ok(domain: str, opportunities: list[dict], *, delta_hours: int, sources: Optional[list[str]] = None) -> dict:
    return {
        "message": "True",
        "domain": domain,
        "delta_hours": delta_hours,
        "sources": sources or [],
        "opportunities": opportunities,
        "count": len(opportunities),
    }


# ---------------------------------------------------------------------------
# Job Boards — sync on purpose: run_all is thread-blocking for many minutes.
# ---------------------------------------------------------------------------
@router.post("/boards")
def run_boards(
    delta_hours: int = Query(default=1, ge=1),
    boards: Optional[list[str]] = Query(default=None),
    limit: Optional[int] = Query(default=None, ge=1),
) -> dict[str, object]:
    try:
        jobs = run_jobs_boards(delta_hours=delta_hours, boards=boards, limit=limit)
    except UnknownBoardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "message": "True",
        "domain": "boards",
        "delta_hours": delta_hours,
        "boards": boards or ["ashby", "greenhouse", "lever"],
        "limit": limit,
        "opportunities": jobs,
        "count": len(jobs),
    }


# Keep the bare ``POST /scan/`` route around for back-compat with anything
# that used to call into it pre-refactor (e.g. the docs README examples).
@router.post("/")
def run_scan_legacy_compat(
    delta_hours: int = Query(default=1, ge=1),
    boards: Optional[list[str]] = Query(default=None),
    limit: Optional[int] = Query(default=None, ge=1),
) -> dict[str, object]:
    return run_boards(delta_hours=delta_hours, boards=boards, limit=limit)


# ---------------------------------------------------------------------------
# Funding News
# ---------------------------------------------------------------------------
@router.post("/funding")
def run_funding(
    delta_hours: int = Query(default=168, ge=1, description="Hours since publish; default 1 week."),
    limit: int = Query(default=50, ge=1),
    sources: Optional[list[str]] = Query(default=None, description="Allowed values: producthunt, startupsgallery."),
) -> dict[str, object]:
    return _ok(
        "funding",
        scan_funding(delta_hours=delta_hours, limit=limit, sources=sources),
        delta_hours=delta_hours,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# NGOs
# ---------------------------------------------------------------------------
@router.post("/ngos")
def run_ngos(
    delta_hours: int = Query(default=72, ge=1, description="Hours since publish; default 3 days."),
    limit: int = Query(default=50, ge=1),
    sources: Optional[list[str]] = Query(default=None, description="Allowed values: reliefweb, idealist."),
) -> dict[str, object]:
    return _ok(
        "ngos",
        scan_ngos(delta_hours=delta_hours, limit=limit, sources=sources),
        delta_hours=delta_hours,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# Remote Jobs
# ---------------------------------------------------------------------------
@router.post("/remote")
def run_remote(
    delta_hours: int = Query(default=24, ge=1, description="Hours since publish; default 24h."),
    limit: int = Query(default=50, ge=1),
    sources: Optional[list[str]] = Query(default=None, description="Allowed values: hackernews, remotive, remoteok."),
) -> dict[str, object]:
    return _ok(
        "remote",
        scan_remote(delta_hours=delta_hours, limit=limit, sources=sources),
        delta_hours=delta_hours,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# Open Source
# ---------------------------------------------------------------------------
@router.post("/oss")
def run_oss(
    delta_hours: int = Query(default=168, ge=1, description="Hours since publish; default 1 week."),
    limit: int = Query(default=50, ge=1),
    sources: Optional[list[str]] = Query(default=None, description="Allowed values: github."),
    languages: Optional[list[str]] = Query(default=None, description="Languages to query GitHub Trending for."),
) -> dict[str, object]:
    opportunities = scan_oss(
        delta_hours=delta_hours,
        limit=limit,
        sources=sources,
        languages=languages,
    )
    return {
        "message": "True",
        "domain": "oss",
        "delta_hours": delta_hours,
        "sources": sources or ["github"],
        "languages": languages or ["python"],
        "opportunities": opportunities,
        "count": len(opportunities),
    }
