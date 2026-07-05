"""Crunchbase scraper.

Scrapes Crunchbase discover page for recently funded companies.
Disabled by default due to aggressive rate limiting.
Uses very conservative request pacing.
"""

import asyncio
import logging

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

CRUNCHBASE_DISCOVER_URL = "https://www.crunchbase.com/discover/organization.companies"
CRUNCHBASE_CATEGORIES = [
    "artificial-intelligence",
    "machine-learning",
    "saas",
    "fintech",
]


class CrunchbaseScraper(BaseScraper):
    name = "crunchbase"
    enabled_setting = "scraper_crunchbase_enabled"

    @with_backoff(max_retries=2, base_delay=5.0)
    async def scrape(self) -> list[dict]:
        companies = []

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }

        resp = await self.http.get(
            CRUNCHBASE_DISCOVER_URL,
            headers=headers,
            params={
                "sort": "last_funding_date",
                "order": "desc",
            },
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        # Parse company cards from the discover page
        rows = soup.select(
            "grid-row, .component--grid-row, tr[class*='row']"
        )

        for row in rows[:30]:  # Conservative limit
            try:
                name_el = row.select_one(
                    "a[class*='identifier'], field-formatter a, .cb-link"
                )
                if not name_el:
                    continue

                name = name_el.get_text(strip=True)
                if not name:
                    continue

                href = name_el.get("href", "")
                website = (
                    f"https://www.crunchbase.com{href}"
                    if href and not href.startswith("http")
                    else href
                )

                # Try to extract funding info
                funding_el = row.select_one(
                    "[class*='funding'], [class*='money']"
                )
                funding_amount = None
                if funding_el:
                    funding_text = funding_el.get_text(strip=True)
                    funding_amount = _parse_funding_amount(funding_text)

                slug = make_slug(name)
                dedup_key = "crunchbase-latest"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                companies.append(
                    {
                        "name": name,
                        "name_slug": slug,
                        "website": website,
                        "source": self.name,
                        "source_url": website,
                        "funding_amount": funding_amount,
                        "funding_stage": "unknown",
                        "raw_data": {"crunchbase_url": website},
                    }
                )
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse Crunchbase row: {e}")

            # Conservative rate limiting between parsing
            await asyncio.sleep(0.5)

        return companies


def _parse_funding_amount(text: str) -> float | None:
    """Parse funding amount from text like '$4.5M' or '$10B'."""
    text = text.strip().replace(",", "").replace("$", "")
    multiplier = 1

    if text.upper().endswith("B"):
        multiplier = 1_000_000_000
        text = text[:-1]
    elif text.upper().endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000
        text = text[:-1]

    try:
        return float(text) * multiplier
    except (ValueError, TypeError):
        return None
