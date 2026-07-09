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
from services.scoring_service import score_and_persist

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


def _score_then_ok(domain: str, opportunities: list[dict], *, delta_hours: int, sources: Optional[list[str]] = None) -> dict:
    """Run the scanner, score every opportunity against the user's profile,
    persist winners above ``preferences.job_fit_threshold``, drop the rest,
    then return the canonical scan-response envelope.

    Scoring is intentionally fire-and-forget from the response's perspective
    — the user sees raw opportunities immediately, and the persisted winners
    show up in ``/api/jobs`` shortly after. If the LLM is misconfigured or
    every provider fails, :func:`score_and_persist` logs a WARN and returns
    ``0`` so the scan response still ships.
    """
    score_and_persist(opportunities, domain)
    return _ok(domain, opportunities, delta_hours=delta_hours, sources=sources)


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

    score_and_persist(jobs, "boards")
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
    return _score_then_ok(
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
    return _score_then_ok(
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
    return _score_then_ok(
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
    score_and_persist(opportunities, "oss")
    return {
        "message": "True",
        "domain": "oss",
        "delta_hours": delta_hours,
        "sources": sources or ["github"],
        "languages": languages or ["python"],
        "opportunities": opportunities,
        "count": len(opportunities),
    }
