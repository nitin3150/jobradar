"""Idealist.org scraper.

Scrapes paid tech jobs from NGOs and nonprofits on Idealist.org.
Uses httpx + BeautifulSoup (server-rendered pages).
"""

import logging

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

IDEALIST_URL = "https://www.idealist.org/en/jobs"
IDEALIST_SEARCH_PARAMS = {
    "q": "software engineer OR developer OR data OR machine learning",
    "jobType": "FULL_TIME",
    "locationType": "REMOTE",
    "sort": "DATE",
}


class IdealistScraper(BaseScraper):
    name = "idealist"
    enabled_setting = "scraper_idealist_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }

        resp = await self.http.get(
            IDEALIST_URL, params=IDEALIST_SEARCH_PARAMS, headers=headers
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        companies = []

        # Idealist job listings are in card-like elements
        job_cards = soup.select(
            "a[data-testid='search-result'], .listing-card, a[class*='ListingCard']"
        )

        if not job_cards:
            # Fallback: try broader selectors
            job_cards = soup.select("a[href*='/job/']")

        for card in job_cards[:30]:
            try:
                # Extract job title
                title_el = card.select_one(
                    "h3, h4, [class*='title'], [class*='Title']"
                )
                job_title = title_el.get_text(strip=True) if title_el else ""

                # Extract org name
                org_el = card.select_one(
                    "[class*='org'], [class*='Org'], [class*='company'], span + span"
                )
                org_name = org_el.get_text(strip=True) if org_el else ""

                if not org_name and not job_title:
                    continue
                name = org_name or job_title

                # Extract location
                loc_el = card.select_one(
                    "[class*='location'], [class*='Location']"
                )
                location = loc_el.get_text(strip=True) if loc_el else "Remote"

                href = card.get("href", "")
                source_url = (
                    f"https://www.idealist.org{href}"
                    if href and not href.startswith("http")
                    else href
                )

                slug = make_slug(f"{name}-{job_title}"[:200])
                dedup_key = "idealist-latest"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                companies.append(
                    {
                        "name": name,
                        "name_slug": slug,
                        "website": source_url,
                        "source": self.name,
                        "source_url": source_url,
                        "description": f"{job_title} at {name}. Location: {location}",
                        "category": "ngo",
                        "likely_roles": [job_title] if job_title else [],
                        "hiring_signals": [f"Active job posting: {job_title}"],
                        "raw_data": {
                            "job_title": job_title,
                            "org_name": org_name,
                            "location": location,
                            "platform": "idealist",
                        },
                    }
                )
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse Idealist listing: {e}")

        return companies
