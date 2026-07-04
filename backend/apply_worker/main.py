"""Apply worker — dequeues jobs from Redis, fills forms via Playwright, submits."""
import asyncio
import json
import logging
import sys
import os

# Add backend/ to path so app.* imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from uuid import UUID

from playwright.async_api import async_playwright
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.qa_bank import QABankEntry
from apply_worker.form_filler import (
    attach_resume,
    extract_form_fields,
    fill_field,
    take_screenshot,
)
from apply_worker.qa_matcher import find_match
from apply_worker.resume_selector import pick_resume_for_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

APPLY_QUEUE = "apply_queue"


async def process_job(job_id: str, page) -> None:
    """Process a single job application."""
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.id == UUID(job_id)))
        job = result.scalar_one_or_none()
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        if job.status not in (JobStatus.APPROVED.value,):
            logger.warning(f"Job {job_id} status is {job.status}, skipping")
            return

        # Fetch Q&A bank entries that have answers
        bank_result = await db.execute(
            select(QABankEntry).where(QABankEntry.answer.isnot(None))
        )
        bank_entries = bank_result.scalars().all()

        # Pick the best-matching resume (may be None if user uploaded none).
        try:
            resume_path = await pick_resume_for_job(db, job)
            if resume_path:
                logger.info(f"Resume selected: {resume_path}")
            else:
                logger.info("No resume on file; skipping file attachment")
        except Exception as e:
            logger.warning(f"Resume selection failed for job {job_id}: {e}")
            resume_path = None

        logger.info(f"Processing job {job_id}: {job.title} @ {job.url}")
        await page.goto(job.url, wait_until="networkidle", timeout=30000)

        # Click "Apply" button if present
        apply_btn = await page.query_selector(
            "a:has-text('Apply'), button:has-text('Apply')"
        )
        if apply_btn:
            await apply_btn.click()
            await page.wait_for_load_state("networkidle")

        # Attach resume file to the form (best-effort).
        if resume_path:
            try:
                await attach_resume(page, resume_path)
            except Exception as e:
                logger.warning(f"attach_resume failed for job {job_id}: {e}")

        fields = await extract_form_fields(page)
        unknown_fields = []
        fill_plan = []

        for field in fields:
            match = find_match(field["label"], bank_entries, settings.qa_match_threshold)
            if match:
                fill_plan.append((field, match))
            else:
                unknown_fields.append(field["label"])

        if unknown_fields:
            # Save unknown questions to bank with answer=null
            async with async_session() as db2:
                for label in unknown_fields:
                    existing = await db2.execute(
                        select(QABankEntry).where(
                            QABankEntry.question_pattern == label.lower().strip()
                        )
                    )
                    if not existing.scalar_one_or_none():
                        entry = QABankEntry(
                            question_pattern=label.lower().strip(),
                            canonical_question=label,
                            answer=None,
                        )
                        db2.add(entry)
                await db2.commit()

            job.status = JobStatus.FLAGGED.value
            await db.commit()
            logger.warning(f"Job {job_id} flagged — unknown fields: {unknown_fields}")
            return

        # Fill all matched fields
        for field, qa_entry in fill_plan:
            await fill_field(page, field, qa_entry.answer)
            qa_entry.times_used += 1
            qa_entry.last_used_at = datetime.now(timezone.utc)

        await db.commit()

        # Submit form (skipped entirely in dry-run mode).
        if settings.apply_dry_run:
            logger.info(f"[DRY RUN] Job {job_id}: skipping submit + APPLIED status")
        else:
            submit_btn = await page.query_selector(
                "button[type=submit], input[type=submit], button:has-text('Submit')"
            )
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_load_state("networkidle")

        screenshot_path = await take_screenshot(page, job_id)

        application = Application(
            job_id=UUID(job_id),
            submission_screenshot_path=screenshot_path,
            status=(
                ApplicationStatus.DRY_RUN.value
                if settings.apply_dry_run
                else ApplicationStatus.SUBMITTED.value
            ),
            notes="dry_run" if settings.apply_dry_run else None,
        )
        db.add(application)
        # In dry-run leave job.status = APPROVED so it is never marked as truly
        # applied; production advances it to APPLIED.
        if not settings.apply_dry_run:
            job.status = JobStatus.APPLIED.value
        await db.commit()
        logger.info(
            f"Job {job_id} {'dry-run recorded' if settings.apply_dry_run else 'submitted successfully'}"
        )


async def main() -> None:
    import redis.asyncio as aioredis

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    logger.info("Apply worker started. Waiting for jobs...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        page = await browser.new_page()

        try:
            while True:
                item = await redis.blpop(APPLY_QUEUE, timeout=5)
                if not item:
                    continue
                _, payload = item
                data = json.loads(payload)
                job_id = data.get("job_id")
                if job_id:
                    try:
                        await process_job(job_id, page)
                    except Exception as e:
                        logger.error(f"Failed to process job {job_id}: {e}")
        finally:
            await browser.close()
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
