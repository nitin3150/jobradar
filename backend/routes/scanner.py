from typing import Optional

from fastapi import APIRouter, Query

from pipeline.nodes.jobs_boards.runner import run_all
from pipeline.nodes.ngo import scan_ngos
from pipeline.nodes.startups.startup_scan import scan_startups

router = APIRouter()


@router.post("/")
async def run_scan(
    delta_hours: int = Query(default=1, ge=1),
    boards: Optional[list[str]] = Query(default=None),
    limit: Optional[int] = Query(default=None, ge=1),
) -> dict[str, object]:
    scraper_results = run_all(delta_hours=delta_hours, boards=boards, limit=limit)

    return {
        "message": "True",
        "res": "",
        "scraper_results": scraper_results,
        "delta_hours": delta_hours,
        "boards": boards or ["ashby", "greenhouse", "lever"],
        "limit": limit,
    }


@router.post("/ngos")
async def run_ngo_scan(limit: int = Query(default=20, ge=1)) -> dict[str, object]:
    result = scan_ngos(limit=limit)
    return {"message": "True", "opportunities": result.get("opportunities", []), "count": len(result.get("opportunities", []))}


@router.post("/startups")
async def run_startup_scan(limit: int = Query(default=20, ge=1)) -> dict[str, object]:
    result = scan_startups(limit=limit)
    return {"message": "True", "opportunities": result.get("opportunities", []), "count": len(result.get("opportunities", []))}