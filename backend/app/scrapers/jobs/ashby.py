"""Ashby job board scraper using public posting API."""
import logging
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger(__name__)

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


async def fetch_ashby_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all open job postings for an Ashby-hosted company.

    Returns list of dicts with keys: title, url, jd_text, ats_type
    """
    try:
        resp = await client.get(
            ASHBY_API.format(slug=slug),
            headers={"User-Agent": "JobRadar/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for posting in data.get("jobPostings", []):
            html = posting.get("descriptionHtml", "") or ""
            jd_text = BeautifulSoup(html, "lxml").get_text(separator="\n", strip=True)
            jobs.append({
                "title": posting.get("title", ""),
                "url": posting.get("jobPostingUrl", ""),
                "jd_text": jd_text,
                "ats_type": "ashby",
            })
        return jobs
    except Exception as e:
        logger.error(f"Ashby scraper failed for {slug}: {e}")
        return []
