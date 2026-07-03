"""Job scraping pipeline — fetches jobs for all ATS-enabled companies, scores, saves."""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.company import Company
from app.models.job import Job, JobStatus
from app.scrapers.jobs.ashby import fetch_ashby_jobs
from app.scrapers.jobs.greenhouse import fetch_greenhouse_jobs
from app.scrapers.jobs.lever import fetch_lever_jobs
from app.scrapers.jobs.scorer import score_job

logger = logging.getLogger(__name__)

# Map ATS type strings to module-level names so patches work in tests
_ATS_TYPES = ("ashby", "lever", "greenhouse")


def _get_fetcher(ats_type: str):
    """Look up fetcher by name at call time (supports test patching)."""
    import app.pipeline.jobs as _self
    return {
        "ashby": _self.fetch_ashby_jobs,
        "lever": _self.fetch_lever_jobs,
        "greenhouse": _self.fetch_greenhouse_jobs,
    }.get(ats_type)


async def run_job_scrape_pipeline(
    db: AsyncSession,
    http_client: httpx.AsyncClient,
) -> int:
    """Scrape jobs for all ATS-enabled companies, score, save new ones.

    Returns count of new jobs saved.
    """
    if not settings.scraper_jobs_enabled:
        logger.info("Job scraper disabled, skipping")
        return 0

    result = await db.execute(
        select(Company).where(
            Company.ats_type.isnot(None),
            Company.ats_slug.isnot(None),
        )
    )
    companies = result.scalars().all()
    logger.info(f"Scraping jobs for {len(companies)} ATS-enabled companies")

    new_count = 0
    skipped_noise = 0
    threshold = settings.job_fit_threshold
    role_terms = [r.lower() for r in settings.target_roles]
    deadline = datetime.now(timezone.utc) + timedelta(hours=settings.review_window_hours)

    for company in companies:
        fetcher = _get_fetcher(company.ats_type)
        if not fetcher:
            continue

        raw_jobs = await fetcher(http_client, company.ats_slug)

        for raw in raw_jobs:
            if not raw.get("url"):
                continue

            # Dedup by URL
            existing = await db.execute(select(Job).where(Job.url == raw["url"]))
            if existing.scalar_one_or_none():
                continue

            # Cheap title prefilter: skip obvious non-matches before spending an LLM call.
            title_lower = raw["title"].lower()
            title_match = any(term in title_lower for term in role_terms)
            if not title_match and threshold > 0:
                skipped_noise += 1
                continue

            score, reasoning = score_job(raw["title"], raw.get("jd_text", ""))

            # Noise gate: drop low-fit roles instead of flooding the review queue.
            if score < threshold:
                skipped_noise += 1
                continue

            job = Job(
                company_id=company.id,
                title=raw["title"],
                url=raw["url"],
                ats_type=raw["ats_type"],
                jd_text=raw.get("jd_text"),
                ai_fit_score=score,
                ai_fit_reasoning=reasoning,
                status=JobStatus.IN_REVIEW.value,
                review_deadline=deadline,
            )
            db.add(job)
            new_count += 1

    await db.commit()
    logger.info(
        f"Job pipeline saved {new_count} new jobs "
        f"(skipped {skipped_noise} below fit threshold {threshold})"
    )
    return new_count
