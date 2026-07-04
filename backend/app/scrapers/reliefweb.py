"""ReliefWeb Jobs API scraper.

Scrapes tech jobs from international/humanitarian organizations using the
ReliefWeb Jobs API v2. v1 was retired (410 Gone). v2 requires an *approved*
appname — register at https://apidoc.reliefweb.int/parameters#appname and set
settings.reliefweb_appname, else the API returns 403.
"""

import logging

from app.config import settings
from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)

RELIEFWEB_API = "https://api.reliefweb.int/v2/jobs"

# Tech-related job categories in ReliefWeb taxonomy
TECH_SEARCH_TERMS = [
    "software", "developer", "engineer", "IT", "data",
    "technology", "digital", "web", "systems", "database",
]


class ReliefWebScraper(BaseScraper):
    name = "reliefweb"
    enabled_setting = "scraper_reliefweb_enabled"

    @with_backoff(max_retries=3, base_delay=2.0)
    async def scrape(self) -> list[dict]:
        companies = []

        for term in TECH_SEARCH_TERMS[:5]:  # Limit to avoid rate issues
            try:
                results = await self._search_jobs(term)
                companies.extend(results)
            except Exception as e:
                logger.warning(f"ReliefWeb search '{term}' failed: {e}")

        # Deduplicate by slug (multiple search terms may match same job)
        seen_slugs = set()
        unique = []
        for c in companies:
            if c["name_slug"] not in seen_slugs:
                seen_slugs.add(c["name_slug"])
                unique.append(c)

        return unique

    async def _search_jobs(self, query: str) -> list[dict]:
        resp = await self.http.post(
            RELIEFWEB_API,
            params={"appname": settings.reliefweb_appname},
            headers={
                "User-Agent": "FundingRadar/1.0",
                "Content-Type": "application/json",
            },
            json={
                "query": {"value": query},
                "filter": {
                    "field": "status",
                    "value": "active",
                },
                "fields": {
                    "include": [
                        "title", "body", "source.name", "source.homepage",
                        "country.name", "date.closing", "date.created",
                        "url",
                    ],
                },
                "sort": ["date.created:desc"],
                "limit": 20,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        companies = []

        for item in data.get("data", []):
            try:
                fields = item.get("fields", {})
                job_title = fields.get("title", "")
                if not job_title:
                    continue

                # Source = the organization posting the job
                source_info = fields.get("source", [])
                org_name = ""
                org_website = None
                if source_info and isinstance(source_info, list):
                    org_name = source_info[0].get("name", "")
                    org_website = source_info[0].get("homepage", "")
                elif isinstance(source_info, dict):
                    org_name = source_info.get("name", "")
                    org_website = source_info.get("homepage", "")

                if not org_name:
                    org_name = job_title.split(" - ")[0] if " - " in job_title else job_title

                # Country
                country_info = fields.get("country", [])
                location = ""
                if country_info and isinstance(country_info, list):
                    location = country_info[0].get("name", "")

                # URL
                source_url = fields.get("url", "")

                # Date
                created = fields.get("date", {}).get("created", "")
                funding_date = created[:10] if created else ""

                slug = make_slug(f"{org_name}-{job_title}"[:200])
                dedup_key = funding_date or "rw-latest"

                if await self.is_duplicate(slug, dedup_key):
                    continue

                # Body excerpt
                body = fields.get("body", "")
                if body:
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
                    body = " ".join(body.split())[:400]

                companies.append({
                    "name": org_name,
                    "name_slug": slug,
                    "website": org_website,
                    "source": self.name,
                    "source_url": source_url,
                    "description": f"{job_title} at {org_name}. Location: {location}. {body}",
                    "category": "ngo",
                    "likely_roles": [job_title],
                    "hiring_signals": [
                        f"Active job posting: {job_title}",
                        f"Location: {location}" if location else "International organization",
                    ],
                    "raw_data": {
                        "job_title": job_title,
                        "org_name": org_name,
                        "location": location,
                        "reliefweb_id": item.get("id"),
                    },
                })
                await self.mark_seen(slug, dedup_key)

            except Exception as e:
                logger.warning(f"Failed to parse ReliefWeb job: {e}")

        return companies
