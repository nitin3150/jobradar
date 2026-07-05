import asyncio
import logging

import httpx
import redis.asyncio as aioredis

from app.config import Settings
from app.scrapers.crunchbase import CrunchbaseScraper
from app.scrapers.hackernews import HackerNewsScraper
from app.scrapers.idealist import IdealistScraper
from app.scrapers.producthunt import ProductHuntScraper
from app.scrapers.reliefweb import ReliefWebScraper
from app.scrapers.sec_edgar import SECEdgarScraper
from app.scrapers.techcrunch_rss import TechCrunchRSSScraper
from app.scrapers.techjobsforgood import TechJobsForGoodScraper
from app.scrapers.twitter_scraper import TwitterScraper
from app.scrapers.unjobs import UNJobsScraper
from app.scrapers.vc_scraper import VCScraper
from app.scrapers.yc_scraper import YCScraper

logger = logging.getLogger(__name__)

ALL_SCRAPERS = [
    # Startup funding scrapers
    SECEdgarScraper,
    YCScraper,
    VCScraper,
    TwitterScraper,
    CrunchbaseScraper,
    HackerNewsScraper,
    TechCrunchRSSScraper,
    ProductHuntScraper,
    # NGO/Nonprofit job scrapers
    IdealistScraper,
    UNJobsScraper,
    TechJobsForGoodScraper,
    ReliefWebScraper,
]


async def run_all_scrapers(
    http_client: httpx.AsyncClient,
    redis: aioredis.Redis,
    settings: Settings,
    browser=None,
) -> list[dict]:
    """Run all enabled scrapers concurrently and return combined results."""
    scrapers = []
    for cls in ALL_SCRAPERS:
        scraper = cls(http_client, redis, settings)
        if cls == VCScraper and browser:
            scraper.set_browser(browser)
        scrapers.append(scraper)

    results = await asyncio.gather(
        *(s.safe_scrape() for s in scrapers), return_exceptions=True
    )

    companies = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Scraper task failed: {result}")
            continue
        companies.extend(result)

    logger.info(f"All scrapers returned {len(companies)} total companies")
    return companies
