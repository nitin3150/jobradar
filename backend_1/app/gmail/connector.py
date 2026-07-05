"""Gmail connector — polls for replies to job applications, updates Application status."""
import logging
import os
import sys
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.gmail.classifier import classify_reply
from app.models.application import Application, ApplicationStatus

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

STATUS_MAP = {
    "interview": ApplicationStatus.INTERVIEW.value,
    "rejection": ApplicationStatus.REJECTED.value,
    "other": None,
}


def _get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds = None
    token_path = settings.gmail_token_path
    creds_path = settings.gmail_credentials_path

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Guard against hanging in headless/Docker environments
            if not sys.stdin.isatty():
                logger.warning("Gmail: No TTY available. Skipping OAuth flow.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            try:
                creds = flow.run_local_server(port=0)
            except Exception as e:
                logger.error(
                    f"Gmail OAuth requires interactive setup. Run locally first: {e}"
                )
                return None
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


async def poll_gmail_replies(db: AsyncSession) -> int:
    """Poll Gmail for new replies to job applications.

    Returns count of applications updated.
    """
    if not os.path.exists(settings.gmail_credentials_path):
        logger.warning("Gmail credentials not configured, skipping poll")
        return 0

    try:
        service = _get_gmail_service()
    except Exception as e:
        logger.error(f"Gmail auth failed: {e}")
        return 0

    if service is None:
        return 0

    label = settings.gmail_label
    updated = 0

    try:
        result = service.users().threads().list(
            userId="me",
            q=f"label:{label} newer_than:1d",
        ).execute()

        threads = result.get("threads", [])

        for thread_data in threads:
            thread_id = thread_data["id"]

            existing = await db.execute(
                select(Application).where(Application.gmail_thread_id == thread_id)
            )
            app = existing.scalar_one_or_none()
            if app:
                continue

            thread = service.users().threads().get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["Subject", "From"],
            ).execute()

            messages = thread.get("messages", [])
            if len(messages) < 2:
                continue

            headers = {
                h["name"]: h["value"]
                for h in messages[-1].get("payload", {}).get("headers", [])
            }
            subject = headers.get("Subject", "")
            snippet = messages[-1].get("snippet", "")

            classification = classify_reply(subject, snippet)
            new_status = STATUS_MAP.get(classification)

            if not new_status:
                continue

            # Load unlinked applications with their job and company eagerly
            unlinked_result = await db.execute(
                select(Application)
                .where(Application.gmail_thread_id.is_(None))
                .options(
                    selectinload(Application.job).selectinload(
                        Application.job.property.mapper.class_.company
                    )
                )
            )
            unlinked_apps = unlinked_result.scalars().all()

            subject_lower = subject.lower()
            target_app = None
            for candidate in unlinked_apps:
                job = candidate.job
                if job is None:
                    continue
                company = job.company
                company_name = company.name if company else ""
                job_title = job.title or ""
                if (
                    company_name and company_name.lower() in subject_lower
                ) or (
                    job_title and job_title.lower() in subject_lower
                ):
                    target_app = candidate
                    break

            if target_app is None:
                logger.warning(
                    f"Could not match Gmail thread {thread_id} to any application"
                )
                continue

            target_app.gmail_thread_id = thread_id
            target_app.status = new_status
            target_app.last_email_at = datetime.now(timezone.utc)
            updated += 1

        await db.commit()
        logger.info(f"Gmail poll complete. Updated {updated} applications.")
        return updated

    except Exception as e:
        logger.error(f"Gmail poll failed: {e}")
        return 0
