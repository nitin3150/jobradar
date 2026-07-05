"""VC portfolio page scraper.

Scrapes portfolio pages of major VCs using Playwright (JS-rendered pages).
Configurable list of VC firms and their portfolio URLs.
"""

import logging

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

# VC portfolio pages to scrape
VC_PORTFOLIO_PAGES = [
    {
        "firm": "a16z",
        "url": "https://a16z.com/portfolio/",
        "selector": "a[class*='portfolio']",
        "name_selector": "h3, h4, .company-name",
    },
    {
        "firm": "sequoia",
        "url": "https://www.sequoiacap.com/our-companies/",
        "selector": "a[class*='company'], .company-card",
        "name_selector": "h3, h4, .company-name",
    },
    {
        "firm": "benchmark",
        "url": "https://www.benchmark.com/portfolio/",
        "selector": ".portfolio-company, a[href*='company']",
        "name_selector": "h3, h4, .name",
    },
    {
        "firm": "greylock",
        "url": "https://greylock.com/portfolio/",
        "selector": ".portfolio-item, a[class*='portfolio']",
        "name_selector": "h3, h4, .company-name",
    },
]


class VCScraper(BaseScraper):
    name = "vc_portfolio"
    enabled_setting = "scraper_vc_portfolio_enabled"

    @with_backoff(max_retries=2, base_delay=3.0)
    async def scrape(self) -> list[dict]:
        # Playwright browser must be available via app.state
        # This scraper is called from the pipeline which passes the browser
        if not hasattr(self, "browser") or self.browser is None:
            logger.warning("No Playwright browser available, skipping VC scraper")
            return []

        companies = []
        for vc in VC_PORTFOLIO_PAGES:
            try:
                vc_companies = await self._scrape_vc_page(vc)
                companies.extend(vc_companies)
            except Exception as e:
                logger.error(f"Failed to scrape {vc['firm']}: {e}")

        return companies

    def set_browser(self, browser):
        """Set the Playwright browser instance."""
        self.browser = browser

    async def _scrape_vc_page(self, vc: dict) -> list[dict]:
        page = await self.browser.new_page()
        try:
            await page.goto(vc["url"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)  # Allow dynamic content to load

            content = await page.content()
            soup = BeautifulSoup(content, "lxml")

            companies = []
            elements = soup.select(vc["selector"])

            for el in elements[:50]:  # Limit to 50 per VC to avoid overwhelm
                try:
                    name_el = el.select_one(vc["name_selector"])
                    name = name_el.get_text(strip=True) if name_el else el.get_text(strip=True)
                    name = name.split("\n")[0].strip()

                    if not name or len(name) < 2:
                        continue

                    href = el.get("href", "")
                    website = href if href.startswith("http") else None

                    slug = make_slug(name)
                    dedup_key = f"{vc['firm']}-latest"

                    if await self.is_duplicate(slug, dedup_key):
                        continue

                    companies.append(
                        {
                            "name": name,
                            "name_slug": slug,
                            "website": website,
                            "source": f"vc_{vc['firm']}",
                            "source_url": vc["url"],
                            "funding_stage": "unknown",
                            "raw_data": {"vc_firm": vc["firm"], "portfolio_url": vc["url"]},
                        }
                    )
                    await self.mark_seen(slug, dedup_key)

                except Exception as e:
                    logger.warning(f"Failed to parse element from {vc['firm']}: {e}")

            return companies
        finally:
            await page.close()
