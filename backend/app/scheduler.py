"""APScheduler configuration for periodic pipeline runs."""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from app.config import settings

logger = logging.getLogger(__name__)


async def run_full_pipeline(app: FastAPI):
    """Scheduled job: run the full funding detection pipeline."""
    from app.pipeline.graph import run_pipeline

    logger.info(f"[Scheduled] Starting full pipeline run at {datetime.now(timezone.utc)}")
    try:
        result = await run_pipeline(
            http_client=app.state.http_client,
            redis=app.state.redis,
            settings=settings,
            browser=getattr(app.state, "browser", None),
        )
        logger.info(f"[Scheduled] Pipeline complete. Stats: {result.get('stats', {})}")
    except Exception as e:
        logger.error(f"[Scheduled] Pipeline failed: {e}")


async def run_twitter_refresh(app: FastAPI):
    """Scheduled job: refresh Twitter hiring signals only."""
    from app.scrapers.twitter_scraper import TwitterScraper

    logger.info(f"[Scheduled] Starting Twitter refresh at {datetime.now(timezone.utc)}")
    try:
        scraper = TwitterScraper(app.state.http_client, app.state.redis, settings)
        signals = await scraper.safe_scrape()
        logger.info(f"[Scheduled] Twitter refresh found {len(signals)} signals")
    except Exception as e:
        logger.error(f"[Scheduled] Twitter refresh failed: {e}")


def start_scheduler(app: FastAPI):
    """Initialize and start APScheduler with configured jobs."""
    scheduler = AsyncIOScheduler()

    # Daily full pipeline run
    scheduler.add_job(
        run_full_pipeline,
        CronTrigger(
            hour=settings.pipeline_schedule_hour,
            minute=0,
            timezone=settings.pipeline_schedule_timezone,
        ),
        kwargs={"app": app},
        id="daily_pipeline",
        replace_existing=True,
    )
    logger.info(
        f"Scheduled daily pipeline at {settings.pipeline_schedule_hour}:00 "
        f"{settings.pipeline_schedule_timezone}"
    )

    # Twitter refresh every N hours (only if enabled)
    if settings.scraper_twitter_enabled:
        scheduler.add_job(
            run_twitter_refresh,
            IntervalTrigger(hours=settings.twitter_refresh_hours),
            kwargs={"app": app},
            id="twitter_refresh",
            replace_existing=True,
        )
        logger.info(
            f"Scheduled Twitter refresh every {settings.twitter_refresh_hours} hours"
        )

    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler started")
