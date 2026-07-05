"""HackerNews 'Who is Hiring' scraper.

Scrapes the monthly "Ask HN: Who is hiring?" threads via the
Algolia HN Search API (free, no auth needed).
"""

import logging
from datetime import datetime, timezone

from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

# Algolia HN Search API
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="

# Keywords that indicate a tech company posting
TECH_KEYWORDS = [
    "engineer", "developer", "python", "react", "backend",
    "frontend", "fullstack", "devops", "machine learning",
    "data scientist", "infrastructure", "platform", "SRE",
    "typescript", "golang", "rust", "kubernetes",
]


class HackerNewsScraper(BaseScraper):
    name = "hackernews"
    enabled_setting = "scraper_hackernews_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        # Find the latest "Who is hiring?" thread
        thread_id = await self._find_latest_thread()
        if not thread_id:
            logger.warning("No 'Who is hiring' thread found")
            return []

        # Fetch comments (job postings) from the thread
        return await self._parse_thread_comments(thread_id)

    async def _find_latest_thread(self) -> int | None:
        """Find the latest 'Ask HN: Who is hiring?' thread."""
        resp = await self.http.get(
            HN_SEARCH_URL,
            # NOTE: Algolia dropped num_comments from numericAttributesForFiltering,
            # so filtering on it server-side now 400s. Filter comment count client-side.
            params={
                "query": "Ask HN: Who is hiring?",
                "tags": "story,ask_hn",
                "hitsPerPage": 10,
            },
            headers={"User-Agent": "FundingRadar/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()

        for hit in data.get("hits", []):
            title = hit.get("title", "").lower()
            if (
                "who is hiring" in title
                and "freelancer" not in title
                and hit.get("num_comments", 0) > 100
            ):
                return hit.get("objectID")

        return None

    async def _parse_thread_comments(self, thread_id: int) -> list[dict]:
        """Fetch and parse top-level comments from a Who is Hiring thread."""
        resp = await self.http.get(
            f"https://hn.algolia.com/api/v1/items/{thread_id}",
            headers={"User-Agent": "FundingRadar/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()

        companies = []
        children = data.get("children", [])

        for comment in children[:80]:  # Top 80 comments
            try:
                text = comment.get("text", "")
                if not text:
                    continue

                # First line is usually "Company Name | Role | Location | ..."
                first_line = text.split("<p>")[0].strip()
                # Strip HTML tags
                first_line = first_line.replace("<br>", " ").strip()
                import re
                first_line = re.sub(r"<[^>]+>", "", first_line)

                parts = [p.strip() for p in first_line.split("|")]
                if len(parts) < 2:
                    continue

                company_name = parts[0].strip()
                if not company_name or len(company_name) > 100:
                    continue

                # Check if this looks like a tech job
                text_lower = text.lower()
                if not any(kw in text_lower for kw in TECH_KEYWORDS):
                    continue

                # Extract roles from the pipe-delimited first line
                roles = []
                location = ""
                website = None
                for part in parts[1:]:
                    part_lower = part.strip().lower()
                    if any(kw in part_lower for kw in ["engineer", "developer", "designer", "manager", "scientist", "lead", "sre", "devops"]):
                        roles.append(part.strip())
                    elif any(kw in part_lower for kw in ["remote", "onsite", "hybrid", "sf", "nyc", "london", "berlin"]):
                        location = part.strip()
                    elif part.strip().startswith("http"):
                        website = part.strip()

                # Extract URL from text if not in header
                if not website:
                    url_match = re.search(r'href="(https?://[^"]+)"', text)
                    if url_match:
                        website = url_match.group(1)

                slug = make_slug(company_name)
                comment_id = comment.get("id", "")
                dedup_key = f"hn-{thread_id}"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                # Clean description (strip HTML)
                desc_text = re.sub(r"<[^>]+>", " ", text)
                desc_text = " ".join(desc_text.split())[:500]

                companies.append({
                    "name": company_name,
                    "name_slug": slug,
                    "website": website,
                    "source": self.name,
                    "source_url": f"{HN_ITEM_URL}{comment_id}",
                    "description": desc_text,
                    "category": "startup",
                    "likely_roles": roles[:5],
                    "hiring_signals": [
                        f"Actively hiring on HN Who is Hiring thread",
                        f"Location: {location}" if location else "Hiring for tech roles",
                    ],
                    "raw_data": {
                        "thread_id": thread_id,
                        "comment_id": comment_id,
                        "first_line": first_line,
                    },
                })
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse HN comment: {e}")
                continue

        return companies
