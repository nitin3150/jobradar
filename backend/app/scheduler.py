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


async def run_job_scraper(app: FastAPI):
    """Hourly job: scrape Ashby/Lever/Greenhouse for new postings."""
    from app.database import async_session
    from app.pipeline.jobs import run_job_scrape_pipeline

    logger.info(f"[Scheduled] Starting job scrape at {datetime.now(timezone.utc)}")
    try:
        async with async_session() as session:
            count = await run_job_scrape_pipeline(session, app.state.http_client)
        logger.info(f"[Scheduled] Job scrape complete. {count} new jobs.")
    except Exception as e:
        logger.error(f"[Scheduled] Job scrape failed: {e}")


async def run_review_deadline_check(app: FastAPI):
    """Every 15 min: expire jobs past review deadline."""
    from app.database import async_session
    from app.models.job import Job, JobStatus
    from sqlalchemy import update

    logger.info(f"[Scheduled] Checking review deadlines at {datetime.now(timezone.utc)}")
    try:
        async with async_session() as session:
            expired_action = settings.review_deadline_action
            new_status = (
                JobStatus.APPROVED.value
                if expired_action == "approve"
                else JobStatus.REJECTED.value
            )
            result = await session.execute(
                update(Job)
                .where(
                    Job.status == JobStatus.IN_REVIEW.value,
                    Job.review_deadline < datetime.now(timezone.utc),
                )
                .values(status=new_status)
                .returning(Job.id)
            )
            expired = result.fetchall()
            await session.commit()
            logger.info(f"[Scheduled] Expired {len(expired)} jobs -> {new_status}")

            # Enqueue approved jobs to Redis apply_queue
            if new_status == JobStatus.APPROVED.value and expired:
                import json
                for (job_id,) in expired:
                    await app.state.redis.rpush(
                        "apply_queue", json.dumps({"job_id": str(job_id)})
                    )
    except Exception as e:
        logger.error(f"[Scheduled] Review deadline check failed: {e}")


async def run_gmail_poll(app: FastAPI):
    """Poll Gmail for application replies every 15 min."""
    from app.database import async_session
    from app.gmail.connector import poll_gmail_replies

    logger.info(f"[Scheduled] Gmail poll at {datetime.now(timezone.utc)}")
    try:
        async with async_session() as session:
            count = await poll_gmail_replies(session)
        logger.info(f"[Scheduled] Gmail poll updated {count} applications")
    except Exception as e:
        logger.error(f"[Scheduled] Gmail poll failed: {e}")


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

    # Hourly job scraper
    scheduler.add_job(
        run_job_scraper,
        IntervalTrigger(hours=1),
        kwargs={"app": app},
        id="hourly_job_scraper",
        replace_existing=True,
    )

    # Review deadline check every 15 min
    scheduler.add_job(
        run_review_deadline_check,
        IntervalTrigger(minutes=15),
        kwargs={"app": app},
        id="review_deadline_check",
        replace_existing=True,
    )

    scheduler.add_job(
        run_gmail_poll,
        IntervalTrigger(minutes=15),
        kwargs={"app": app},
        id="gmail_poll",
        replace_existing=True,
    )

    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler started")
