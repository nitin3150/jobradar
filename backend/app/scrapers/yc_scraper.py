"""Y Combinator batch scraper.

Scrapes the YC company directory for the latest batch companies.
Uses httpx + BeautifulSoup (server-rendered page).
"""

import logging

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

YC_COMPANIES_URL = "https://www.ycombinator.com/companies"
# Latest batches to scrape
YC_BATCHES = ["W25", "S24"]


class YCScraper(BaseScraper):
    name = "yc"
    enabled_setting = "scraper_yc_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        companies = []

        for batch in YC_BATCHES:
            batch_companies = await self._scrape_batch(batch)
            companies.extend(batch_companies)

        return companies

    async def _scrape_batch(self, batch: str) -> list[dict]:
        params = {"batch": batch}
        resp = await self.http.get(
            YC_COMPANIES_URL,
            params=params,
            headers={"User-Agent": "FundingRadar/1.0"},
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        companies = []

        # YC company directory uses structured div elements
        company_links = soup.select("a[class*='_company_']")
        if not company_links:
            # Fallback: try alternate selector patterns
            company_links = soup.select("a[href^='/companies/']")

        for link in company_links:
            try:
                name_el = link.select_one("span[class*='_coName_'], .company-name, h4")
                name = name_el.get_text(strip=True) if name_el else ""
                if not name:
                    # Try getting text directly
                    name = link.get_text(strip=True).split("\n")[0].strip()
                if not name:
                    continue

                desc_el = link.select_one(
                    "span[class*='_coDescription_'], .company-description, p"
                )
                description = desc_el.get_text(strip=True) if desc_el else None

                href = link.get("href", "")
                website = f"https://www.ycombinator.com{href}" if href else None

                slug = make_slug(name)
                funding_date = f"2025-{batch}"  # Approximate

                if await self.is_duplicate(slug, funding_date):
                    continue

                companies.append(
                    {
                        "name": name,
                        "name_slug": slug,
                        "website": website,
                        "description": description,
                        "source": self.name,
                        "source_url": website,
                        "funding_stage": "seed",
                        "funding_date": funding_date,
                        "raw_data": {"batch": batch, "yc_url": website},
                    }
                )
                await self.mark_seen(slug, funding_date)

            except Exception as e:
                logger.warning(f"Failed to parse YC company element: {e}")
                continue

        return companies
