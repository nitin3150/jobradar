import logging
from abc import ABC, abstractmethod
from datetime import timedelta

import httpx
import redis.asyncio as aioredis

from app.config import Settings

logger = logging.getLogger(__name__)

DEDUP_TTL = timedelta(days=30)


class BaseScraper(ABC):
    """Abstract base class for all funding scrapers."""

    name: str = "base"
    enabled_setting: str = ""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        redis: aioredis.Redis,
        settings: Settings,
    ):
        self.http = http_client
        self.redis = redis
        self.settings = settings

    @property
    def is_enabled(self) -> bool:
        if not self.enabled_setting:
            return True
        return getattr(self.settings, self.enabled_setting, False)

    async def is_duplicate(self, name_slug: str, funding_date: str) -> bool:
        key = f"company:{name_slug}:{funding_date}"
        return await self.redis.exists(key) > 0

    async def mark_seen(self, name_slug: str, funding_date: str) -> None:
        key = f"company:{name_slug}:{funding_date}"
        await self.redis.setex(key, DEDUP_TTL, "1")

    @abstractmethod
    async def scrape(self) -> list[dict]:
        """Scrape funding data and return list of raw company dicts.

        Each dict should have at minimum:
        - name: str
        - source: str
        - raw_data: dict (original scraped payload)

        Optional fields: website, funding_amount, funding_stage, funding_date,
        source_url, description, founder_name, founder_twitter, founder_linkedin
        """
        ...

    async def safe_scrape(self) -> list[dict]:
        """Run scrape() with error handling. Returns empty list on failure."""
        if not self.is_enabled:
            logger.info(f"Scraper {self.name} is disabled, skipping")
            return []
        try:
            results = await self.scrape()
            logger.info(f"Scraper {self.name} found {len(results)} companies")
            return results
        except Exception as e:
            logger.error(f"Scraper {self.name} failed: {e}")
            return []
