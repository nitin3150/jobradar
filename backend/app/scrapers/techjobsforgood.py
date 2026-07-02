"""Tech Jobs for Good scraper.

Scrapes curated tech roles at nonprofits and mission-driven organizations
from techjobsforgood.com.
Uses httpx + BeautifulSoup.
"""

import logging

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

TJFG_URL = "https://www.techjobsforgood.com"


class TechJobsForGoodScraper(BaseScraper):
    name = "techjobsforgood"
    enabled_setting = "scraper_techjobsforgood_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }

        resp = await self.http.get(TJFG_URL, headers=headers)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        companies = []

        # Tech Jobs for Good job listings
        job_cards = soup.select(
            ".job-listing, .job-card, a[href*='/listing/'], "
            "[class*='JobCard'], [class*='job-row'], article"
        )

        if not job_cards:
            # Broader fallback
            job_cards = soup.select("a[href*='job'], a[href*='listing']")

        for card in job_cards[:30]:
            try:
                # Extract job title
                title_el = card.select_one(
                    "h2, h3, h4, [class*='title'], [class*='Title']"
                )
                job_title = title_el.get_text(strip=True) if title_el else ""

                # Extract organization name
                org_el = card.select_one(
                    "[class*='company'], [class*='org'], [class*='Company'], "
                    "[class*='Org'], span[class*='name']"
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

                # Extract description snippet
                desc_el = card.select_one(
                    "p, [class*='description'], [class*='snippet']"
                )
                description = desc_el.get_text(strip=True) if desc_el else ""

                href = card.get("href", "")
                if not href:
                    link = card.select_one("a[href]")
                    href = link.get("href", "") if link else ""

                source_url = (
                    f"{TJFG_URL}{href}"
                    if href and not href.startswith("http")
                    else href
                ) or TJFG_URL

                slug = make_slug(f"{name}-{job_title}"[:200])
                dedup_key = "tjfg-latest"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                companies.append(
                    {
                        "name": name,
                        "name_slug": slug,
                        "website": source_url,
                        "source": self.name,
                        "source_url": source_url,
                        "description": description
                        or f"{job_title} at {name}. Location: {location}",
                        "category": "ngo",
                        "likely_roles": [job_title] if job_title else [],
                        "hiring_signals": [f"Tech for Good role: {job_title}"],
                        "raw_data": {
                            "job_title": job_title,
                            "org_name": org_name,
                            "location": location,
                            "platform": "techjobsforgood",
                        },
                    }
                )
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse TJFG listing: {e}")

        return companies
