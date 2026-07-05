"""Product Hunt scraper.

Scrapes recently launched products from Product Hunt's public pages.
Catches early-stage startups before they appear on Crunchbase.
No API key needed — scrapes the public page.
"""

import logging
import re

from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

PH_URL = "https://www.producthunt.com"


class ProductHuntScraper(BaseScraper):
    name = "producthunt"
    enabled_setting = "scraper_producthunt_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }

        resp = await self.http.get(PH_URL, headers=headers, follow_redirects=True)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        companies = []

        # Product Hunt lists products in structured elements
        # Try multiple selectors as PH updates their markup
        product_items = soup.select(
            "[data-test='post-item'], [class*='PostItem'], "
            "div[class*='styles_item'], a[href^='/posts/']"
        )

        if not product_items:
            # Fallback: look for links to product pages
            product_items = soup.select("a[href*='/posts/']")

        seen_names = set()

        for item in product_items[:40]:
            try:
                # Extract product name
                name_el = item.select_one(
                    "h3, [data-test='post-name'], [class*='title'], "
                    "[class*='Title'], strong"
                )
                name = name_el.get_text(strip=True) if name_el else ""

                if not name:
                    # Try getting from link text
                    name = item.get_text(strip=True).split("\n")[0].strip()

                if not name or len(name) > 100 or name in seen_names:
                    continue
                seen_names.add(name)

                # Extract tagline/description
                desc_el = item.select_one(
                    "[data-test='post-tagline'], [class*='tagline'], "
                    "[class*='Tagline'], p, span"
                )
                tagline = desc_el.get_text(strip=True) if desc_el else ""

                # Extract link
                href = ""
                if item.name == "a":
                    href = item.get("href", "")
                else:
                    link_el = item.select_one("a[href*='/posts/']")
                    if link_el:
                        href = link_el.get("href", "")

                source_url = f"{PH_URL}{href}" if href and not href.startswith("http") else href

                # Extract vote count as a signal of traction
                vote_el = item.select_one(
                    "[data-test='vote-button'] span, [class*='vote'], "
                    "[class*='Vote'], button span"
                )
                votes = 0
                if vote_el:
                    vote_text = vote_el.get_text(strip=True)
                    vote_match = re.search(r"\d+", vote_text)
                    if vote_match:
                        votes = int(vote_match.group())

                slug = make_slug(name)
                dedup_key = "ph-today"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                companies.append({
                    "name": name,
                    "name_slug": slug,
                    "website": source_url,
                    "source": self.name,
                    "source_url": source_url,
                    "description": tagline or f"New product launch on Product Hunt: {name}",
                    "funding_stage": "pre-seed",
                    "category": "startup",
                    "likely_roles": ["Full Stack Engineer", "Growth"],
                    "hiring_signals": [
                        f"Just launched on Product Hunt{f' ({votes} upvotes)' if votes else ''}",
                        "Early-stage startup likely to hire soon",
                    ],
                    "raw_data": {
                        "tagline": tagline,
                        "votes": votes,
                        "ph_url": source_url,
                    },
                })
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse Product Hunt item: {e}")

        return companies
