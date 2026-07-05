"""Playwright-based browser scraper for direct careers pages."""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List

from scanner.providers.base import Job

logger = logging.getLogger(__name__)

# Common selectors for well-known ATS platforms
PLATFORM_SELECTORS = {
    "greenhouse": {
        "card": "div.opening",
        "title": "a",
        "url": "a",
        "location": "span.location",
    },
    "lever": {
        "card": "div.posting",
        "title": "a[data-qa='serp-job-title']",
        "url": "a",
        "location": "span.sort-by-location",
    },
    "ashby": {
        "card": "div[class*='JobPosting']",
        "title": "a",
        "url": "a",
        "location": "span",
    },
    "generic": {
        "card": "div.job-card, li.job-item, .job-listing, [class*='job']",
        "title": "h2, h3, .job-title, a",
        "url": "a",
        "location": ".location, .job-location, [class*='location']",
    },
}


async def fetch_playwright(entry: Dict) -> List[Job]:
    """Use Playwright to scrape a company's careers page directly."""
    from playwright.async_api import async_playwright

    url = entry.get("careers_url")
    if not url:
        return []

    ats_type = entry.get("ats_type", "generic").lower()
    selectors = PLATFORM_SELECTORS.get(ats_type, PLATFORM_SELECTORS["generic"])

    jobs = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle")

            # Handle infinite scroll where applicable
            for _ in range(10):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)

            cards = await page.query_selector_all(selectors["card"])
            for card in cards:
                try:
                    title_el = await card.query_selector(selectors["title"])
                    url_el = await card.query_selector(selectors["url"])
                    loc_el = await card.query_selector(selectors["location"])

                    title = await title_el.inner_text() if title_el else ""
                    job_url = await url_el.get_attribute("href") if url_el else ""
                    location = await loc_el.inner_text() if loc_el else ""

                    # Resolve relative URLs
                    if job_url and job_url.startswith("/"):
                        from urllib.parse import urljoin

                        job_url = urljoin(url, job_url)

                    if title and job_url:
                        jobs.append(
                            Job(
                                title=title.strip(),
                                url=job_url,
                                company=entry.get("name", ""),
                                location=location.strip() if location else "",
                                posted_at=None,
                                ats_type=ats_type,
                            )
                        )
                except Exception:
                    continue

            await browser.close()
    except Exception as e:
        logger.error(f"Playwright scraper failed for {entry.get('name')}: {e}")

    return jobs
