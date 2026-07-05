"""UN Jobs scraper.

Scrapes tech-related positions from United Nations and international
organizations via unjobs.org.
Uses httpx + BeautifulSoup.
"""

import logging

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

UNJOBS_SEARCH_URL = "https://unjobs.org/search"
TECH_KEYWORDS = [
    "software",
    "developer",
    "engineer",
    "data scientist",
    "IT",
    "technology",
    "digital",
    "cybersecurity",
]


class UNJobsScraper(BaseScraper):
    name = "unjobs"
    enabled_setting = "scraper_unjobs_enabled"

    @with_backoff(max_retries=3, base_delay=3.0)
    async def scrape(self) -> list[dict]:
        companies = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html",
        }

        # Search for tech-related UN jobs
        for keyword in TECH_KEYWORDS[:3]:
            try:
                batch = await self._search_keyword(keyword, headers)
                companies.extend(batch)
            except Exception as e:
                logger.warning(f"UN Jobs search failed for '{keyword}': {e}")

        return companies

    async def _search_keyword(self, keyword: str, headers: dict) -> list[dict]:
        resp = await self.http.get(
            UNJOBS_SEARCH_URL,
            params={"q": keyword, "sort": "date"},
            headers=headers,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        results = []

        # UN Jobs listings
        job_rows = soup.select(
            "table tr, .job-listing, a[href*='/duty_stations/'], .search-result"
        )
        if not job_rows:
            job_rows = soup.select("a[href*='unjobs.org/']")

        for row in job_rows[:15]:
            try:
                # Extract job title
                title_el = row.select_one("a, h3, h4, td:first-child")
                job_title = title_el.get_text(strip=True) if title_el else ""
                if not job_title or len(job_title) < 5:
                    continue

                # Extract organization
                org_el = row.select_one(
                    "td:nth-child(2), [class*='org'], [class*='agency']"
                )
                org_name = org_el.get_text(strip=True) if org_el else "United Nations"

                # Extract duty station
                loc_el = row.select_one(
                    "td:nth-child(3), [class*='location'], [class*='duty']"
                )
                location = loc_el.get_text(strip=True) if loc_el else ""

                # Extract deadline
                deadline_el = row.select_one(
                    "td:last-child, [class*='deadline'], [class*='date']"
                )
                deadline = deadline_el.get_text(strip=True) if deadline_el else ""

                href = ""
                link = row.select_one("a[href]") or row
                if link.get("href"):
                    href = link["href"]

                source_url = (
                    f"https://unjobs.org{href}"
                    if href and not href.startswith("http")
                    else href
                )

                slug = make_slug(f"{org_name}-{job_title}"[:200])
                dedup_key = "unjobs-latest"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                results.append(
                    {
                        "name": org_name,
                        "name_slug": slug,
                        "website": source_url,
                        "source": self.name,
                        "source_url": source_url,
                        "description": f"{job_title} at {org_name}. Duty station: {location}",
                        "category": "ngo",
                        "likely_roles": [job_title],
                        "hiring_signals": [f"UN job posting: {job_title}"],
                        "raw_data": {
                            "job_title": job_title,
                            "org_name": org_name,
                            "location": location,
                            "deadline": deadline,
                            "platform": "unjobs",
                        },
                    }
                )
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse UN Jobs row: {e}")

        return results
