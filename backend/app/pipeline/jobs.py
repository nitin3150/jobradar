"""Job scraping pipeline — fetches jobs for all ATS-enabled companies, scores, saves."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.company import Company
from app.models.job import Job, JobStatus
from app.models.preferences import Preferences
from app.resumes.profile import build_candidate_profile
from app.scrapers.jobs.ashby import fetch_ashby_jobs
from app.scrapers.jobs.greenhouse import fetch_greenhouse_jobs
from app.scrapers.jobs.lever import fetch_lever_jobs
from app.scrapers.jobs.scorer import score_job

logger = logging.getLogger(__name__)

# Map ATS type strings to module-level names so patches work in tests
_ATS_TYPES = ("ashby", "lever", "greenhouse")

# Cap concurrent board fetches so we don't hammer the ATS APIs / open too many sockets.
_FETCH_CONCURRENCY = 10


def resolve_prefs(prefs, defaults) -> tuple[list[str], float, float]:
    """Resolve effective (target_roles, job_fit_threshold, review_window_hours).

    Uses the DB Preferences row when present; falls back to `defaults` (the env
    `settings`). Empty DB target_roles fall back to defaults so the title
    prefilter is never blanked out.
    """
    if prefs is not None:
        roles = list(prefs.target_roles) or list(defaults.target_roles)
        return roles, prefs.job_fit_threshold, prefs.review_window_hours
    return list(defaults.target_roles), defaults.job_fit_threshold, defaults.review_window_hours


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
    prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
    role_names, threshold, review_window_hours = resolve_prefs(prefs, settings)
    role_terms = [r.lower() for r in role_names]
    deadline = datetime.now(timezone.utc) + timedelta(hours=review_window_hours)

    # Build the candidate profile once per run (resume text + target roles).
    # Guarded: a profile-build failure must not abort the whole run — fall back
    # to an empty profile so score_job uses its default.
    try:
        profile = await build_candidate_profile(db, prefs=prefs)
    except Exception as e:
        logger.warning(f"Candidate profile build failed, using default: {e}")
        profile = ""

    # Phase 1: fetch every board concurrently (network-bound, safe to parallelize).
    # DB writes stay sequential below — a single AsyncSession is not concurrency-safe.
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch(company):
        fetcher = _get_fetcher(company.ats_type)
        if not fetcher:
            return company, []
        async with sem:
            try:
                return company, await fetcher(http_client, company.ats_slug)
            except Exception as e:
                logger.error(f"Fetch failed for {company.ats_type}:{company.ats_slug}: {e}")
                return company, []

    fetched = await asyncio.gather(*[_fetch(c) for c in companies])

    # Phase 2: dedup + score + save (sequential — shared DB session + blocking LLM call).
    for company, raw_jobs in fetched:
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

            score, reasoning = score_job(raw["title"], raw.get("jd_text", ""), profile)

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
