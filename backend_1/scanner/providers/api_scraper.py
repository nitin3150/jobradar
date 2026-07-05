"""API scraper providers for known ATS platforms (Greenhouse, Lever, Ashby, etc.)."""

import logging
from datetime import datetime
from typing import Dict, List

import httpx

from scanner.providers.base import Job

logger = logging.getLogger(__name__)


# ----- Greenhouse API -----
async def _greenhouse_fetch(company: str) -> List[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"User-Agent": "JobRadar/1.0"})
        resp.raise_for_status()
        data = resp.json()

    jobs = []
    for posting in data.get("jobs", []):
        posted_str = posting.get("updated_at") or posting.get("created_at", "")
        posted_dt = None
        if posted_str:
            try:
                posted_dt = datetime.fromisoformat(posted_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        jobs.append(
            Job(
                title=posting.get("title", ""),
                url=posting.get("absolute_url", ""),
                company=company,
                location=posting.get("location", {}).get("name", "")
                if isinstance(posting.get("location"), dict)
                else "",
                posted_at=posted_dt,
                ats_type="greenhouse",
            )
        )
    return jobs


# ----- Lever API -----
async def _lever_fetch(company: str) -> List[Job]:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"User-Agent": "JobRadar/1.0"})
        resp.raise_for_status()
        data = resp.json()

    jobs = []
    for posting in data or []:
        posted_str = posting.get("createdAt", "")
        posted_dt = None
        if posted_str:
            try:
                posted_dt = datetime.fromisoformat(posted_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        jobs.append(
            Job(
                title=posting.get("text", ""),
                url=posting.get("hostedUrl", "") or posting.get("applyUrl", ""),
                company=company,
                location=positions[0].get("location", "")
                if (posting.get("categories") and posting["categories"].get("location"))
                else "",
                posted_at=posted_dt,
                ats_type="lever",
            )
        )
    return jobs


# ----- Ashby API -----
async def _ashby_fetch(company: str) -> List[Job]:
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": company},
        "query": """
        query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
          jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
            jobPostings {
              id
              title
              locationName
              employmentType
              createdAt
            }
          }
        }
        """,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "JobRadar/1.0",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    jobs = []
    board = data.get("data", {}).get("jobBoardWithTeams", {})
    for posting in board.get("jobPostings", []) or []:
        created_at = posting.get("createdAt")
        posted_dt = None
        if created_at:
            try:
                posted_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                pass

        jobs.append(
            Job(
                title=posting.get("title", ""),
                url=f"https://jobs.ashbyhq.com/{company}/{posting.get('id', '')}",
                company=company,
                location=posting.get("locationName", ""),
                posted_at=posted_dt,
                ats_type="ashby",
            )
        )
    return jobs


# ----- Registry -----
PROVIDER_HANDLERS = {
    "greenhouse": _greenhouse_fetch,
    "lever": _lever_fetch,
    "ashby": _ashby_fetch,
}


async def fetch_api(entry: Dict) -> List[Job]:
    """Fetch jobs via the platform's public API."""
    ats_type = entry.get("ats_type", "").lower()
    ats_slug = entry.get("ats_slug", entry.get("name", ""))
    handler = PROVIDER_HANDLERS.get(ats_type)
    if not handler:
        logger.warning(f"No API handler for ATS type: {ats_type}")
        return []
    return await handler(ats_slug)
