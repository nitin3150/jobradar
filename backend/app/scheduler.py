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


async def run_ats_discovery(app: FastAPI):
    """Daily: discover new ATS company slugs via site: search, then attach them."""
    from app.database import async_session
    from app.pipeline.discovery import run_discovery_pipeline

    logger.info(f"[Scheduled] Starting ATS discovery at {datetime.now(timezone.utc)}")
    try:
        async with async_session() as session:
            found = await run_discovery_pipeline(
                session,
                app.state.http_client,
                browser=getattr(app.state, "browser", None),
            )
        logger.info(f"[Scheduled] ATS discovery attached {found} new company slugs")
    except Exception as e:
        logger.error(f"[Scheduled] ATS discovery failed: {e}")


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


# Redis key for the runtime-configurable job-fetch interval + scheduler job id.
SCHEDULE_KEY = "config:job_fetch_interval_hours"
JOB_FETCH_ID = "hourly_job_scraper"


async def get_fetch_interval(redis) -> int:
    """Read persisted fetch interval (hours) from redis, falling back to config."""
    try:
        raw = await redis.get(SCHEDULE_KEY)
        if raw is not None:
            hours = int(raw)
            if hours > 0:
                return hours
    except Exception as e:
        logger.warning(f"Failed to read fetch interval from redis: {e}")
    return settings.job_fetch_interval_hours


async def set_fetch_interval(app: FastAPI, hours: int) -> None:
    """Persist the fetch interval and reschedule the running job-fetch job."""
    if hours <= 0:
        raise ValueError("interval hours must be positive")
    await app.state.redis.set(SCHEDULE_KEY, hours)
    app.state.scheduler.reschedule_job(
        JOB_FETCH_ID, trigger=IntervalTrigger(hours=hours)
    )
    logger.info(f"Rescheduled job fetch loop to every {hours} hour(s)")


def start_scheduler(app: FastAPI, fetch_interval_hours: int = 1):
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

    # Job scraper on a runtime-configurable interval (default from redis/config)
    scheduler.add_job(
        run_job_scraper,
        IntervalTrigger(hours=fetch_interval_hours),
        kwargs={"app": app},
        id=JOB_FETCH_ID,
        replace_existing=True,
    )
    logger.info(f"Scheduled job fetch loop every {fetch_interval_hours} hour(s)")

    # ATS slug discovery — isolated daily loop (grows the company set)
    if settings.discovery_enabled:
        scheduler.add_job(
            run_ats_discovery,
            IntervalTrigger(hours=settings.discovery_interval_hours),
            kwargs={"app": app},
            id="ats_discovery",
            replace_existing=True,
        )
        logger.info(
            f"Scheduled ATS discovery every {settings.discovery_interval_hours} hour(s)"
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
