"""Greenhouse job board scraper using public boards API."""
import logging
import httpx

logger = logging.getLogger(__name__)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


async def fetch_greenhouse_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all open job postings for a Greenhouse-hosted company."""
    try:
        resp = await client.get(
            GREENHOUSE_API.format(slug=slug),
            headers={"User-Agent": "JobRadar/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for posting in data.get("jobs", []):
            jobs.append({
                "title": posting.get("title", ""),
                "url": posting.get("absolute_url", ""),
                "jd_text": posting.get("content", "") or "",
                "ats_type": "greenhouse",
            })
        return jobs
    except Exception as e:
        logger.error(f"Greenhouse scraper failed for {slug}: {e}")
        return []
