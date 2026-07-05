"""Lever job board scraper using public v0 API."""
import logging
import httpx

logger = logging.getLogger(__name__)

LEVER_API = "https://api.lever.co/v0/postings/{slug}"


async def fetch_lever_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all open job postings for a Lever-hosted company."""
    try:
        resp = await client.get(
            LEVER_API.format(slug=slug),
            params={"mode": "json"},
            headers={"User-Agent": "JobRadar/1.0"},
        )
        resp.raise_for_status()
        jobs = []
        for posting in resp.json():
            jd_text = posting.get("descriptionPlain", "") or posting.get("description", "") or ""
            jobs.append({
                "title": posting.get("text", ""),
                "url": posting.get("hostedUrl", ""),
                "jd_text": jd_text,
                "ats_type": "lever",
            })
        return jobs
    except Exception as e:
        logger.error(f"Lever scraper failed for {slug}: {e}")
        return []
