"""Web search scraper for broad discovery via search engines."""

import html
import logging
import re
from typing import Dict, List

import httpx

from scanner.providers.base import Job

logger = logging.getLogger(__name__)

DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"


def _extract_job_info(html_text: str, url: str) -> Dict:
    """Try to extract job title and company from search result HTML."""
    # Try "Title @ Company" or "Title | Company" or "Title at Company"
    for pattern in [
        r"(.+?)(?:\s*[@|—–-]\s*|\s+at\s+)(.+?)(?:\s*[-–]\s*|\s*$)",
        r"([^|]+)\|(.+)",
    ]:
        m = re.search(pattern, html_text, re.IGNORECASE)
        if m:
            return {"title": m.group(1).strip(), "company": m.group(2).strip()}
    return {"title": "", "company": "UNKNOWN"}


async def search_duckduckgo(query: str) -> List[str]:
    """Return result URLs for a DuckDuckGo query."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(DUCKDUCKGO_HTML, params={"q": query})
        resp.raise_for_status()
    # Extract href attributes from result links
    results = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"', resp.text)
    return [html.unescape(u) for u in results]


async def fetch_websearch(entry: Dict, queries: List[Dict]) -> List[Job]:
    """Run web search queries and parse results into Job objects."""
    jobs = []
    for q in queries:
        query = q.get("query", "")
        if not query:
            continue

        # Expand {company} placeholder
        if "{company}" in query:
            company_name = entry.get("company", entry.get("name", ""))
            query = query.replace("{company}", company_name)

        try:
            urls = await search_duckduckgo(query)
            for url in urls:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        page_resp = await client.get(url)
                        page_resp.raise_for_status()

                    info = _extract_job_info(page_resp.text, url)
                    if info["title"]:  # Only add if we extracted a title
                        jobs.append(
                            Job(
                                title=info["title"],
                                url=url,
                                company=info["company"],
                                location="",
                                posted_at=None,
                                ats_type="websearch",
                            )
                        )
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Web search failed for query '{query}': {e}")

    return jobs
