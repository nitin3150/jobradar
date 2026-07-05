"""TechCrunch RSS funding scraper.

Parses TechCrunch RSS feeds for funding round announcements.
No API key needed — just RSS/XML parsing.
"""

import logging
import re
from xml.etree import ElementTree

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

# TechCrunch category feeds for funding news
TC_FEEDS = [
    "https://techcrunch.com/category/fundraising/feed/",
    "https://techcrunch.com/category/startups/feed/",
]

# Keywords that indicate a funding article
FUNDING_KEYWORDS = [
    "raises", "raised", "funding", "series a", "series b", "series c",
    "seed round", "pre-seed", "million", "venture", "investment",
    "closes", "secures", "round", "$",
]

STAGE_PATTERNS = {
    "pre-seed": r"pre.?seed",
    "seed": r"\bseed\b",
    "series-a": r"series\s*a",
    "series-b": r"series\s*b",
    "series-c": r"series\s*[c-z]",
}


def _extract_amount(text: str) -> float | None:
    """Extract funding amount from text like '$5M' or '$5 million'."""
    match = re.search(r"\$(\d+(?:\.\d+)?)\s*(?:million|m\b)", text, re.IGNORECASE)
    if match:
        return float(match.group(1)) * 1_000_000
    match = re.search(r"\$(\d+(?:\.\d+)?)\s*(?:billion|b\b)", text, re.IGNORECASE)
    if match:
        return float(match.group(1)) * 1_000_000_000
    return None


def _detect_stage(text: str) -> str:
    """Detect funding stage from article text."""
    text_lower = text.lower()
    for stage, pattern in STAGE_PATTERNS.items():
        if re.search(pattern, text_lower):
            return stage
    return "unknown"


def _extract_company_name(title: str) -> str:
    """Extract company name from article title like 'Acme raises $5M...'."""
    # Common patterns: "CompanyName raises/secures/closes..."
    match = re.match(r"^([A-Z][\w\s&'.]+?)\s+(?:raises|raised|secures|closes|lands|gets|nabs|bags)", title)
    if match:
        return match.group(1).strip()
    # Fallback: take first few words before a verb
    match = re.match(r"^([A-Z][\w\s&'.]+?),?\s+(?:a |an |the )", title)
    if match:
        return match.group(1).strip()
    return ""


class TechCrunchRSSScraper(BaseScraper):
    name = "techcrunch"
    enabled_setting = "scraper_techcrunch_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        companies = []

        for feed_url in TC_FEEDS:
            try:
                feed_companies = await self._parse_feed(feed_url)
                companies.extend(feed_companies)
            except Exception as e:
                logger.warning(f"Failed to parse TC feed {feed_url}: {e}")

        return companies

    async def _parse_feed(self, feed_url: str) -> list[dict]:
        resp = await self.http.get(
            feed_url,
            headers={
                "User-Agent": "FundingRadar/1.0",
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )
        resp.raise_for_status()

        # Parse XML
        root = ElementTree.fromstring(resp.text)

        # Handle RSS namespace
        items = root.findall(".//item")
        companies = []

        for item in items[:30]:
            try:
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                description = item.findtext("description", "").strip()
                pub_date = item.findtext("pubDate", "").strip()

                # Check if this is a funding article
                combined = f"{title} {description}".lower()
                if not any(kw in combined for kw in FUNDING_KEYWORDS):
                    continue

                # Extract company name from title
                company_name = _extract_company_name(title)
                if not company_name:
                    continue

                # Extract funding details
                amount = _extract_amount(f"{title} {description}")
                stage = _detect_stage(f"{title} {description}")

                # Parse date
                funding_date = ""
                if pub_date:
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(pub_date)
                        funding_date = dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass

                slug = make_slug(company_name)
                dedup_key = funding_date or "tc-latest"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                # Clean description HTML
                clean_desc = re.sub(r"<[^>]+>", "", description)
                clean_desc = " ".join(clean_desc.split())[:500]

                companies.append({
                    "name": company_name,
                    "name_slug": slug,
                    "source": self.name,
                    "source_url": link,
                    "description": clean_desc or title,
                    "funding_amount": amount,
                    "funding_stage": stage,
                    "funding_date": funding_date,
                    "category": "startup",
                    "hiring_signals": [f"Just announced funding: {title}"],
                    "raw_data": {
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                    },
                })
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse TC RSS item: {e}")

        return companies
