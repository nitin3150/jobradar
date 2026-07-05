"""SEC EDGAR Form D scraper.

Uses the free EDGAR full-text search API (EFTS) to find recent Form D filings,
which indicate new securities offerings (funding rounds).
No API key required — just a descriptive User-Agent header.
"""

import logging
from datetime import date, timedelta

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FULL_TEXT_URL = "https://efts.sec.gov/LATEST/search-index"
USER_AGENT = "FundingRadar research@fundingradar.dev"


class SECEdgarScraper(BaseScraper):
    name = "sec_edgar"
    enabled_setting = "scraper_sec_edgar_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        today = date.today()
        # Use a wider window — EDGAR data can lag several days
        start_date = today - timedelta(days=30)

        params = {
            "q": '"form d"',
            "forms": "D",
            "dateRange": "custom",
            "startdt": start_date.isoformat(),
            "enddt": today.isoformat(),
        }
        headers = {"User-Agent": USER_AGENT}

        resp = await self.http.get(EFTS_SEARCH_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        companies = []
        hits = data.get("hits", {}).get("hits", [])

        for hit in hits:
            source = hit.get("_source", {})
            name = None
            display_names = source.get("display_names", [])
            if display_names:
                name = display_names[0]
                # Strip CIK suffix like "Company Name  (CIK 0001234567)"
                if "(CIK" in name:
                    name = name.split("(CIK")[0].strip()
            if not name:
                name = source.get("entity_name", "")
            if not name:
                continue

            slug = make_slug(name)
            filing_date = source.get("file_date", today.isoformat())

            # Skip duplicates
            if await self.is_duplicate(slug, filing_date):
                continue

            company = {
                "name": name,
                "name_slug": slug,
                "source": self.name,
                "source_url": f"https://www.sec.gov/cgi-bin/browse-edgar?company={name}&CIK=&type=D&dateb=&owner=include&count=10&search_text=&action=getcompany",
                "funding_date": filing_date,
                "funding_stage": _guess_stage_from_amount(source),
                "funding_amount": _extract_amount(source),
                "raw_data": source,
            }
            companies.append(company)
            await self.mark_seen(slug, filing_date)

        return companies


def _extract_amount(source: dict) -> float | None:
    """Try to extract total offering amount from filing data."""
    for key in ("total_offering_amount", "total_amount_sold"):
        val = source.get(key)
        if val:
            try:
                return float(str(val).replace(",", "").replace("$", ""))
            except (ValueError, TypeError):
                continue
    return None


def _guess_stage_from_amount(source: dict) -> str:
    """Rough heuristic to guess funding stage from amount."""
    amount = _extract_amount(source)
    if amount is None:
        return "unknown"
    if amount < 1_000_000:
        return "pre-seed"
    if amount < 5_000_000:
        return "seed"
    if amount < 20_000_000:
        return "series-a"
    return "series-b"
