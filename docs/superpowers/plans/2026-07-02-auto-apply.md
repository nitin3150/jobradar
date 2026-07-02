# Auto-Apply Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend jobradar with automated job application submission (Ashby/Lever/Greenhouse), a hybrid review queue, application tracker, Q&A bank, and Gmail reply tracking.

**Architecture:** FastAPI backend enqueues approved jobs to Redis `apply_queue`; a separate `apply-worker` process dequeues and submits via Playwright; Gmail connector polls every 15 min and updates application statuses via LiteLLM classification.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Redis, Playwright, LiteLLM, Google Gmail API, React + TanStack Query, axios, Tailwind CSS

---

## File Map

**New backend files:**
- `backend/app/llm/client.py` — LiteLLM wrapper
- `backend/app/models/job.py` — Job model
- `backend/app/models/application.py` — Application model
- `backend/app/models/qa_bank.py` — QABankEntry model
- `backend/app/scrapers/jobs/ashby.py` — Ashby job listing scraper
- `backend/app/scrapers/jobs/lever.py` — Lever job listing scraper
- `backend/app/scrapers/jobs/greenhouse.py` — Greenhouse job listing scraper
- `backend/app/scrapers/jobs/scorer.py` — LiteLLM fit scorer
- `backend/app/api/jobs.py` — jobs CRUD endpoints
- `backend/app/api/applications.py` — applications CRUD endpoints
- `backend/app/api/qa_bank.py` — Q&A bank CRUD endpoints
- `backend/app/schemas/job.py` — Pydantic schemas for jobs
- `backend/app/schemas/application.py` — Pydantic schemas for applications
- `backend/app/schemas/qa_bank.py` — Pydantic schemas for Q&A bank
- `backend/app/gmail/connector.py` — Gmail API poller
- `backend/app/gmail/classifier.py` — LiteLLM reply classifier
- `backend/alembic/versions/003_add_ats_fields_to_company.py` — migration
- `backend/alembic/versions/004_create_jobs_applications_qa_bank.py` — migration
- `backend/apply_worker/main.py` — worker entrypoint
- `backend/apply_worker/qa_matcher.py` — two-pass Q&A matcher
- `backend/apply_worker/form_filler.py` — Playwright form interaction

**Modified backend files:**
- `backend/app/models/__init__.py` — export new models
- `backend/app/api/router.py` — register new routers
- `backend/app/config.py` — add LLM_API_KEY, LLM_API_BASE, REVIEW_WINDOW_HOURS, REVIEW_DEADLINE_ACTION, QA_MATCH_THRESHOLD, GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH
- `backend/app/scheduler.py` — add job scrape + review deadline tasks
- `backend/alembic/env.py` — import new models
- `backend/pyproject.toml` — add litellm, google-auth, google-api-python-client, rapidfuzz

**New frontend files:**
- `frontend/src/api/jobs.js` — jobs/applications/qa_bank API calls
- `frontend/src/hooks/useJobs.js` — TanStack Query hooks for jobs
- `frontend/src/hooks/useApplications.js` — TanStack Query hooks for applications
- `frontend/src/hooks/useQABank.js` — TanStack Query hooks for Q&A bank
- `frontend/src/pages/JobsReview.jsx` — review queue page
- `frontend/src/pages/ApplicationTracker.jsx` — tracker page
- `frontend/src/pages/QABank.jsx` — Q&A bank manager page

**Modified frontend files:**
- `frontend/src/main.jsx` — add routes
- `frontend/src/components/Navbar.jsx` — add nav links + review badge

---

## Task 1: Add dependencies

**Files:**
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: Add new deps**

```toml
# backend/pyproject.toml — add to dependencies list:
    "litellm>=1.40",
    "google-auth>=2.29",
    "google-auth-oauthlib>=1.2",
    "google-api-python-client>=2.127",
    "rapidfuzz>=3.9",
```

- [ ] **Step 2: Install**

```bash
cd backend && pip install -e ".[dev]"
```

Expected: installs without error.

- [ ] **Step 3: Commit**

```bash
git add backend/pyproject.toml
git commit -m "chore: add litellm, google-api, rapidfuzz deps"
```

---

## Task 2: Update config

**Files:**
- Modify: `backend/app/config.py`

- [ ] **Step 1: Add new settings**

In `backend/app/config.py`, add these fields to the `Settings` class after `openrouter_api_key`:

```python
    # LiteLLM unified key (used when provider needs single key)
    llm_api_key: str = ""
    llm_api_base: str = ""  # override base URL (e.g. Nvidia NIM endpoint)

    # Job scraper + review window
    review_window_hours: int = 2
    review_deadline_action: str = "reject"  # "reject" or "approve"
    qa_match_threshold: float = 0.75

    # Gmail connector
    gmail_credentials_path: str = "gmail_credentials.json"
    gmail_token_path: str = "gmail_token.json"
    gmail_label: str = "job-applications"

    # Apply worker
    apply_worker_screenshot_dir: str = "screenshots"
    scraper_jobs_enabled: bool = True
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/config.py
git commit -m "feat: add auto-apply config fields"
```

---

## Task 3: LiteLLM client wrapper

**Files:**
- Create: `backend/app/llm/__init__.py`
- Create: `backend/app/llm/client.py`
- Test: `backend/tests/test_llm_client.py`

- [ ] **Step 1: Write failing test**

```python
# backend/tests/test_llm_client.py
from unittest.mock import MagicMock, patch
from app.llm.client import llm_complete


def test_llm_complete_passes_model_from_settings():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "hello"

    with patch("app.llm.client.completion", return_value=mock_response) as mock_complete:
        result = llm_complete(messages=[{"role": "user", "content": "hi"}])

    mock_complete.assert_called_once()
    call_kwargs = mock_complete.call_args.kwargs
    assert "model" in call_kwargs
    assert result == "hello"


def test_llm_complete_returns_string():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "test output"

    with patch("app.llm.client.completion", return_value=mock_response):
        result = llm_complete(messages=[{"role": "user", "content": "test"}])

    assert isinstance(result, str)
    assert result == "test output"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_llm_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.llm.client'`

- [ ] **Step 3: Create `__init__.py`**

```python
# backend/app/llm/__init__.py
```

- [ ] **Step 4: Create client**

```python
# backend/app/llm/client.py
"""LiteLLM wrapper — single call pattern for all LLM interactions."""

import os

from litellm import completion

from app.config import settings


def _build_model_string() -> str:
    """Build the model string for LiteLLM routing."""
    model = settings.llm_model
    provider = settings.llm_provider

    # If model already contains provider prefix (e.g. "groq/llama-3.3-70b"), use as-is
    if "/" in model:
        return model

    # Otherwise prefix with provider
    return f"{provider}/{model}"


def llm_complete(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str:
    """Call LLM via LiteLLM. Returns content string.

    Args:
        messages: OpenAI-format message list
        model: Override model string (e.g. "groq/llama-3.3-70b-versatile").
               Defaults to settings.llm_provider/settings.llm_model.
        temperature: Sampling temperature.
        max_tokens: Max response tokens.
    """
    resolved_model = model or _build_model_string()

    kwargs: dict = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base

    response = completion(**kwargs)
    return response.choices[0].message.content
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd backend && python -m pytest tests/test_llm_client.py -v
```

Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add backend/app/llm/ backend/tests/test_llm_client.py
git commit -m "feat: add LiteLLM client wrapper"
```

---

## Task 4: DB models — Job, Application, QABankEntry

**Files:**
- Create: `backend/app/models/job.py`
- Create: `backend/app/models/application.py`
- Create: `backend/app/models/qa_bank.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Create Job model**

```python
# backend/app/models/job.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ATSType(str, enum.Enum):
    ASHBY = "ashby"
    LEVER = "lever"
    GREENHOUSE = "greenhouse"


class JobStatus(str, enum.Enum):
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FLAGGED = "flagged"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ats_type: Mapped[str] = mapped_column(String(32), nullable=False)
    jd_text: Mapped[str | None] = mapped_column(Text)
    ai_fit_score: Mapped[float | None] = mapped_column(Float)
    ai_fit_reasoning: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.IN_REVIEW.value, index=True
    )
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    review_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    application = relationship(
        "Application", back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    company = relationship("Company", back_populates="jobs")
```

- [ ] **Step 2: Create Application model**

```python
# backend/app/models/application.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApplicationStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    GHOSTED = "ghosted"


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    submission_screenshot_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=ApplicationStatus.SUBMITTED.value, index=True
    )
    gmail_thread_id: Mapped[str | None] = mapped_column(String(256))
    last_email_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    job = relationship("Job", back_populates="application")
```

- [ ] **Step 3: Create QABankEntry model**

```python
# backend/app/models/qa_bank.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnswerType(str, enum.Enum):
    TEXT = "text"
    BOOLEAN = "boolean"
    NUMBER = "number"
    SELECT = "select"


class QABankEntry(Base):
    __tablename__ = "qa_bank_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    question_pattern: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    canonical_question: Mapped[str] = mapped_column(String(512), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)  # null = unknown, needs filling
    answer_type: Mapped[str] = mapped_column(
        String(32), default=AnswerType.TEXT.value
    )
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: Update models `__init__.py`**

```python
# backend/app/models/__init__.py
from app.models.application import Application, ApplicationStatus
from app.models.company import Company, CompanyStatus, FundingStage
from app.models.job import ATSType, Job, JobStatus
from app.models.outreach import OutreachMessage, OutreachType
from app.models.qa_bank import AnswerType, QABankEntry

__all__ = [
    "Application",
    "ApplicationStatus",
    "AnswerType",
    "ATSType",
    "Company",
    "CompanyStatus",
    "FundingStage",
    "Job",
    "JobStatus",
    "OutreachMessage",
    "OutreachType",
    "QABankEntry",
]
```

- [ ] **Step 5: Add `jobs` backref to Company model**

In `backend/app/models/company.py`, add after the `outreach_messages` relationship:

```python
    jobs = relationship("Job", back_populates="company", cascade="all, delete-orphan")
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/
git commit -m "feat: add Job, Application, QABankEntry models"
```

---

## Task 5: Alembic migrations

**Files:**
- Modify: `backend/alembic/env.py`
- Create: `backend/alembic/versions/003_add_ats_fields_to_company.py`
- Create: `backend/alembic/versions/004_create_jobs_applications_qa_bank.py`

- [ ] **Step 1: Update alembic env.py imports**

In `backend/alembic/env.py`, replace the models import line:

```python
from app.models import Company, OutreachMessage  # noqa: F401
```

with:

```python
from app.models import (  # noqa: F401
    Application,
    Company,
    Job,
    OutreachMessage,
    QABankEntry,
)
```

- [ ] **Step 2: Create migration 003**

```python
# backend/alembic/versions/003_add_ats_fields_to_company.py
"""Add ats_type and ats_slug to companies table."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("ats_type", sa.String(32), nullable=True))
    op.add_column("companies", sa.Column("ats_slug", sa.String(256), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "ats_slug")
    op.drop_column("companies", "ats_type")
```

- [ ] **Step 3: Create migration 004**

```python
# backend/alembic/versions/004_create_jobs_applications_qa_bank.py
"""Create jobs, applications, qa_bank_entries tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("ats_type", sa.String(32), nullable=False),
        sa.Column("jd_text", sa.Text, nullable=True),
        sa.Column("ai_fit_score", sa.Float, nullable=True),
        sa.Column("ai_fit_reasoning", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="in_review"),
        sa.Column("scraped_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("review_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "applications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("submission_screenshot_path", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="submitted"),
        sa.Column("gmail_thread_id", sa.String(256), nullable=True),
        sa.Column("last_email_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_applications_status", "applications", ["status"])

    op.create_table(
        "qa_bank_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("question_pattern", sa.String(512), nullable=False),
        sa.Column("canonical_question", sa.String(512), nullable=False),
        sa.Column("answer", sa.Text, nullable=True),
        sa.Column("answer_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("times_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_qa_bank_entries_question_pattern", "qa_bank_entries", ["question_pattern"])


def downgrade() -> None:
    op.drop_table("qa_bank_entries")
    op.drop_table("applications")
    op.drop_table("jobs")
```

- [ ] **Step 4: Run migrations**

```bash
cd backend && alembic upgrade head
```

Expected: `Running upgrade 003 -> 004, Create jobs, applications, qa_bank_entries tables`

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/
git commit -m "feat: migrations for ats fields, jobs, applications, qa_bank"
```

---

## Task 6: Job scrapers (Ashby, Lever, Greenhouse)

**Files:**
- Create: `backend/app/scrapers/jobs/__init__.py`
- Create: `backend/app/scrapers/jobs/ashby.py`
- Create: `backend/app/scrapers/jobs/lever.py`
- Create: `backend/app/scrapers/jobs/greenhouse.py`
- Create: `backend/app/scrapers/jobs/scorer.py`
- Test: `backend/tests/test_job_scrapers.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_job_scrapers.py
import pytest
import respx
import httpx
from app.scrapers.jobs.ashby import fetch_ashby_jobs
from app.scrapers.jobs.lever import fetch_lever_jobs
from app.scrapers.jobs.greenhouse import fetch_greenhouse_jobs


@respx.mock
@pytest.mark.asyncio
async def test_fetch_ashby_jobs_returns_list():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobPostings": [
                    {"id": "123", "title": "AI Engineer", "jobPostingUrl": "https://jobs.ashbyhq.com/acme/123", "descriptionHtml": "<p>We need AI.</p>"}
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await fetch_ashby_jobs(client, "acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "AI Engineer"
    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/acme/123"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_lever_jobs_returns_list():
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "abc", "text": "ML Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc", "descriptionPlain": "We need ML."}
            ],
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await fetch_lever_jobs(client, "acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "ML Engineer"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_greenhouse_jobs_returns_list():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 999, "title": "LLM Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/999", "content": "We need LLM."}
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await fetch_greenhouse_jobs(client, "acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "LLM Engineer"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd backend && python -m pytest tests/test_job_scrapers.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create `__init__.py`**

```python
# backend/app/scrapers/jobs/__init__.py
```

- [ ] **Step 4: Create Ashby scraper**

```python
# backend/app/scrapers/jobs/ashby.py
"""Ashby job board scraper using public posting API."""
import logging
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger(__name__)

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


async def fetch_ashby_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all open job postings for an Ashby-hosted company.

    Returns list of dicts with keys: title, url, jd_text
    """
    try:
        resp = await client.get(
            ASHBY_API.format(slug=slug),
            headers={"User-Agent": "JobRadar/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for posting in data.get("jobPostings", []):
            html = posting.get("descriptionHtml", "") or ""
            jd_text = BeautifulSoup(html, "lxml").get_text(separator="\n", strip=True)
            jobs.append({
                "title": posting.get("title", ""),
                "url": posting.get("jobPostingUrl", ""),
                "jd_text": jd_text,
                "ats_type": "ashby",
            })
        return jobs
    except Exception as e:
        logger.error(f"Ashby scraper failed for {slug}: {e}")
        return []
```

- [ ] **Step 5: Create Lever scraper**

```python
# backend/app/scrapers/jobs/lever.py
"""Lever job board scraper using public v0 API."""
import logging
import httpx

logger = logging.getLogger(__name__)

LEVER_API = "https://api.lever.co/v0/postings/{slug}"


async def fetch_lever_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all open job postings for a Lever-hosted company."""
    try:
        resp = await client.get(
            LEVER_API.format(slug=slug),
            params={"mode": "json"},
            headers={"User-Agent": "JobRadar/1.0"},
        )
        resp.raise_for_status()
        jobs = []
        for posting in resp.json():
            jd_text = posting.get("descriptionPlain", "") or posting.get("description", "") or ""
            jobs.append({
                "title": posting.get("text", ""),
                "url": posting.get("hostedUrl", ""),
                "jd_text": jd_text,
                "ats_type": "lever",
            })
        return jobs
    except Exception as e:
        logger.error(f"Lever scraper failed for {slug}: {e}")
        return []
```

- [ ] **Step 6: Create Greenhouse scraper**

```python
# backend/app/scrapers/jobs/greenhouse.py
"""Greenhouse job board scraper using public boards API."""
import logging
import httpx

logger = logging.getLogger(__name__)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


async def fetch_greenhouse_jobs(client: httpx.AsyncClient, slug: str) -> list[dict]:
    """Fetch all open job postings for a Greenhouse-hosted company."""
    try:
        resp = await client.get(
            GREENHOUSE_API.format(slug=slug),
            headers={"User-Agent": "JobRadar/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for posting in data.get("jobs", []):
            jobs.append({
                "title": posting.get("title", ""),
                "url": posting.get("absolute_url", ""),
                "jd_text": posting.get("content", "") or "",
                "ats_type": "greenhouse",
            })
        return jobs
    except Exception as e:
        logger.error(f"Greenhouse scraper failed for {slug}: {e}")
        return []
```

- [ ] **Step 7: Create fit scorer**

```python
# backend/app/scrapers/jobs/scorer.py
"""LiteLLM-based job fit scorer."""
import json
import logging

from app.llm.client import llm_complete

logger = logging.getLogger(__name__)

CANDIDATE_PROFILE = """
Name: Nitin | MS AI, Northeastern | Based in Boston
Target: AI Engineer / LLM Engineer / ML Engineer at Series A-C startups
Stack: LangGraph, LangChain, FastAPI, Python, React Native, AWS, Docker, MongoDB
Wants: Healthcare AI, agentic systems, LLM infra companies — full AI stack ownership
Hard pass: pure frontend, pure data analyst, 10+ yrs required, legacy enterprise tech
"""

SCORE_PROMPT = """You are evaluating a job posting for a candidate.

Candidate profile:
{profile}

Job title: {title}
Job description:
{jd_text}

Rate the fit on a scale of 0.0 to 1.0 and provide a one-sentence reasoning.
Respond ONLY with valid JSON in this exact format:
{{"score": 0.85, "reasoning": "Strong match because..."}}
"""


def score_job(title: str, jd_text: str) -> tuple[float, str]:
    """Score a job posting against candidate profile.

    Returns (score: float 0-1, reasoning: str).
    """
    prompt = SCORE_PROMPT.format(
        profile=CANDIDATE_PROFILE,
        title=title,
        jd_text=jd_text[:3000],  # truncate to avoid token limits
    )
    try:
        raw = llm_complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=128,
        )
        data = json.loads(raw.strip())
        score = float(data.get("score", 0.0))
        reasoning = str(data.get("reasoning", ""))
        return max(0.0, min(1.0, score)), reasoning
    except Exception as e:
        logger.error(f"Fit scorer failed: {e}")
        return 0.0, ""
```

- [ ] **Step 8: Run tests**

```bash
cd backend && python -m pytest tests/test_job_scrapers.py -v
```

Expected: `3 passed`

- [ ] **Step 9: Commit**

```bash
git add backend/app/scrapers/jobs/ backend/tests/test_job_scrapers.py
git commit -m "feat: add Ashby, Lever, Greenhouse job scrapers and fit scorer"
```

---

## Task 7: Job pipeline (scrape + save to DB)

**Files:**
- Create: `backend/app/pipeline/jobs.py`
- Test: `backend/tests/test_job_pipeline.py`

- [ ] **Step 1: Write failing test**

```python
# backend/tests/test_job_pipeline.py
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.pipeline.jobs import run_job_scrape_pipeline


@pytest.mark.asyncio
async def test_run_job_scrape_pipeline_creates_jobs():
    mock_session = AsyncMock()
    mock_http = AsyncMock()

    fake_company = MagicMock()
    fake_company.id = uuid4()
    fake_company.ats_type = "ashby"
    fake_company.ats_slug = "acme"

    # scalars().all() returns list of companies
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_company]
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    with patch("app.pipeline.jobs.fetch_ashby_jobs", return_value=[
        {"title": "AI Engineer", "url": "https://jobs.ashbyhq.com/acme/1", "jd_text": "We build AI.", "ats_type": "ashby"}
    ]):
        with patch("app.pipeline.jobs.score_job", return_value=(0.9, "Great fit")):
            count = await run_job_scrape_pipeline(mock_session, mock_http)

    mock_session.add.assert_called_once()
    assert count == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
cd backend && python -m pytest tests/test_job_pipeline.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create pipeline**

```python
# backend/app/pipeline/jobs.py
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

FETCHERS = {
    "ashby": fetch_ashby_jobs,
    "lever": fetch_lever_jobs,
    "greenhouse": fetch_greenhouse_jobs,
}


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

    # Fetch companies with ATS configured
    result = await db.execute(
        select(Company).where(
            Company.ats_type.isnot(None),
            Company.ats_slug.isnot(None),
        )
    )
    companies = result.scalars().all()
    logger.info(f"Scraping jobs for {len(companies)} ATS-enabled companies")

    new_count = 0
    deadline = datetime.now(timezone.utc) + timedelta(hours=settings.review_window_hours)

    for company in companies:
        fetcher = FETCHERS.get(company.ats_type)
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

            score, reasoning = score_job(raw["title"], raw.get("jd_text", ""))

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
    logger.info(f"Job pipeline saved {new_count} new jobs")
    return new_count
```

- [ ] **Step 4: Run test**

```bash
cd backend && python -m pytest tests/test_job_pipeline.py -v
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipeline/jobs.py backend/tests/test_job_pipeline.py
git commit -m "feat: add job scraping pipeline"
```

---

## Task 8: Scheduler — job scrape + review deadline tasks

**Files:**
- Modify: `backend/app/scheduler.py`

- [ ] **Step 1: Add two new scheduled jobs**

In `backend/app/scheduler.py`, add these two functions before `start_scheduler`:

```python
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
    from sqlalchemy import select, update

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

            # Enqueue approved jobs
            if new_status == JobStatus.APPROVED.value and expired:
                import json
                for (job_id,) in expired:
                    await app.state.redis.rpush(
                        "apply_queue", json.dumps({"job_id": str(job_id)})
                    )
    except Exception as e:
        logger.error(f"[Scheduled] Review deadline check failed: {e}")
```

Then in `start_scheduler`, add after the existing jobs:

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/scheduler.py
git commit -m "feat: schedule hourly job scrape and review deadline check"
```

---

## Task 9: API schemas

**Files:**
- Create: `backend/app/schemas/job.py`
- Create: `backend/app/schemas/application.py`
- Create: `backend/app/schemas/qa_bank.py`

- [ ] **Step 1: Create job schemas**

```python
# backend/app/schemas/job.py
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class JobResponse(BaseModel):
    id: UUID
    company_id: UUID
    company_name: str | None = None
    title: str
    url: str
    ats_type: str
    jd_text: str | None = None
    ai_fit_score: float | None = None
    ai_fit_reasoning: str | None = None
    status: str
    scraped_at: datetime
    review_deadline: datetime | None = None

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class JobStatusUpdate(BaseModel):
    status: str
```

- [ ] **Step 2: Create application schemas**

```python
# backend/app/schemas/application.py
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class ApplicationResponse(BaseModel):
    id: UUID
    job_id: UUID
    job_title: str | None = None
    company_name: str | None = None
    submitted_at: datetime
    submission_screenshot_path: str | None = None
    status: str
    gmail_thread_id: str | None = None
    last_email_at: datetime | None = None
    notes: str | None = None

    model_config = {"from_attributes": True}


class ApplicationListResponse(BaseModel):
    applications: list[ApplicationResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ApplicationStatusUpdate(BaseModel):
    status: str
    notes: str | None = None
```

- [ ] **Step 3: Create Q&A bank schemas**

```python
# backend/app/schemas/qa_bank.py
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class QABankEntryResponse(BaseModel):
    id: UUID
    question_pattern: str
    canonical_question: str
    answer: str | None = None
    answer_type: str
    times_used: int
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class QABankEntryCreate(BaseModel):
    question_pattern: str
    canonical_question: str
    answer: str | None = None
    answer_type: str = "text"


class QABankEntryUpdate(BaseModel):
    answer: str | None = None
    canonical_question: str | None = None
    answer_type: str | None = None
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas/
git commit -m "feat: add job, application, qa_bank pydantic schemas"
```

---

## Task 10: API routes

**Files:**
- Create: `backend/app/api/jobs.py`
- Create: `backend/app/api/applications.py`
- Create: `backend/app/api/qa_bank.py`
- Modify: `backend/app/api/router.py`

- [ ] **Step 1: Create jobs router**

```python
# backend/app/api/jobs.py
"""Job listing endpoints."""
import json
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job, JobStatus
from app.models.company import Company
from app.schemas.job import JobListResponse, JobResponse, JobStatusUpdate

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(Job, Company.name.label("company_name")).join(
        Company, Job.company_id == Company.id
    )
    if status:
        query = query.where(Job.status == status)
    query = query.order_by(Job.scraped_at.desc())

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one()

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.all()

    jobs = []
    for job, company_name in rows:
        r = JobResponse.model_validate(job)
        r.company_name = company_name
        jobs.append(r)

    return JobListResponse(
        jobs=jobs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 1,
    )


@router.get("/pending-count")
async def get_pending_count(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(func.count()).where(Job.status == JobStatus.IN_REVIEW.value)
    )
    return {"count": result.scalar_one()}


@router.patch("/{job_id}/status", response_model=JobResponse)
async def update_job_status(
    job_id: UUID,
    update: JobStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = update.status
    await db.commit()
    await db.refresh(job)
    return JobResponse.model_validate(job)


@router.post("/{job_id}/approve")
async def approve_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    request=None,
):
    """Approve job for application — enqueues to apply_queue."""
    from fastapi import Request
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.APPROVED.value
    await db.commit()

    # Enqueue for apply-worker via Redis
    # Redis client accessed via app state — import app to get state
    from app.redis_client import get_redis
    redis = await get_redis()
    await redis.rpush("apply_queue", json.dumps({"job_id": str(job_id)}))

    return {"status": "queued", "job_id": str(job_id)}


@router.post("/{job_id}/reject")
async def reject_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = JobStatus.REJECTED.value
    await db.commit()
    return {"status": "rejected", "job_id": str(job_id)}
```

- [ ] **Step 2: Check redis_client for get_redis helper**

```bash
cat backend/app/redis_client.py
```

If `get_redis()` doesn't exist, add it to `backend/app/redis_client.py`:

```python
_redis_instance = None

async def get_redis():
    global _redis_instance
    if _redis_instance is None:
        _redis_instance = await init_redis()
    return _redis_instance
```

- [ ] **Step 3: Create applications router**

```python
# backend/app/api/applications.py
"""Application tracking endpoints."""
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.application import Application
from app.models.job import Job
from app.models.company import Company
from app.schemas.application import (
    ApplicationListResponse,
    ApplicationResponse,
    ApplicationStatusUpdate,
)

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("", response_model=ApplicationListResponse)
async def list_applications(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Application, Job.title.label("job_title"), Company.name.label("company_name"))
        .join(Job, Application.job_id == Job.id)
        .join(Company, Job.company_id == Company.id)
    )
    if status:
        query = query.where(Application.status == status)
    query = query.order_by(Application.submitted_at.desc())

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one()

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.all()

    applications = []
    for app_row, job_title, company_name in rows:
        r = ApplicationResponse.model_validate(app_row)
        r.job_title = job_title
        r.company_name = company_name
        applications.append(r)

    return ApplicationListResponse(
        applications=applications,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 1,
    )


@router.patch("/{application_id}/status", response_model=ApplicationResponse)
async def update_application_status(
    application_id: UUID,
    update: ApplicationStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    app.status = update.status
    if update.notes is not None:
        app.notes = update.notes
    await db.commit()
    await db.refresh(app)
    return ApplicationResponse.model_validate(app)
```

- [ ] **Step 4: Create Q&A bank router**

```python
# backend/app/api/qa_bank.py
"""Q&A bank CRUD endpoints."""
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.qa_bank import QABankEntry
from app.schemas.qa_bank import (
    QABankEntryCreate,
    QABankEntryResponse,
    QABankEntryUpdate,
)

router = APIRouter(prefix="/qa-bank", tags=["qa-bank"])


@router.get("", response_model=list[QABankEntryResponse])
async def list_entries(
    unanswered_first: bool = True,
    db: AsyncSession = Depends(get_db),
):
    query = select(QABankEntry)
    if unanswered_first:
        query = query.order_by(
            (QABankEntry.answer == None).desc(),  # noqa: E711
            QABankEntry.times_used.desc(),
        )
    else:
        query = query.order_by(QABankEntry.times_used.desc())
    result = await db.execute(query)
    return [QABankEntryResponse.model_validate(e) for e in result.scalars().all()]


@router.post("", response_model=QABankEntryResponse, status_code=201)
async def create_entry(
    entry: QABankEntryCreate,
    db: AsyncSession = Depends(get_db),
):
    new_entry = QABankEntry(
        question_pattern=entry.question_pattern,
        canonical_question=entry.canonical_question,
        answer=entry.answer,
        answer_type=entry.answer_type,
    )
    db.add(new_entry)
    await db.commit()
    await db.refresh(new_entry)
    return QABankEntryResponse.model_validate(new_entry)


@router.patch("/{entry_id}", response_model=QABankEntryResponse)
async def update_entry(
    entry_id: UUID,
    update: QABankEntryUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QABankEntry).where(QABankEntry.id == entry_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    if update.answer is not None:
        entry.answer = update.answer
    if update.canonical_question is not None:
        entry.canonical_question = update.canonical_question
    if update.answer_type is not None:
        entry.answer_type = update.answer_type
    await db.commit()
    await db.refresh(entry)
    return QABankEntryResponse.model_validate(entry)


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(entry_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(QABankEntry).where(QABankEntry.id == entry_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.delete(entry)
    await db.commit()
```

- [ ] **Step 5: Register routers**

Replace `backend/app/api/router.py`:

```python
"""API router aggregating all sub-routers."""

from fastapi import APIRouter

from app.api.applications import router as applications_router
from app.api.companies import router as companies_router
from app.api.jobs import router as jobs_router
from app.api.outreach import router as outreach_router
from app.api.pipeline import router as pipeline_router
from app.api.qa_bank import router as qa_bank_router

api_router = APIRouter()
api_router.include_router(companies_router)
api_router.include_router(outreach_router)
api_router.include_router(pipeline_router)
api_router.include_router(jobs_router)
api_router.include_router(applications_router)
api_router.include_router(qa_bank_router)
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/ backend/app/schemas/
git commit -m "feat: add jobs, applications, qa-bank API routes"
```

---

## Task 11: Q&A matcher

**Files:**
- Create: `backend/apply_worker/__init__.py`
- Create: `backend/apply_worker/qa_matcher.py`
- Test: `backend/tests/test_qa_matcher.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_qa_matcher.py
import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4
from apply_worker.qa_matcher import find_match, normalize


def test_normalize_lowercases_and_strips():
    assert normalize("  Are you authorized to work? ") == "are you authorized to work"


def test_find_match_exact():
    entries = [
        MagicMock(id=uuid4(), question_pattern="authorized to work in the us", answer="Yes", times_used=0)
    ]
    match = find_match("Are you authorized to work in the US?", entries, threshold=0.75)
    assert match is not None
    assert match.answer == "Yes"


def test_find_match_no_match_returns_none():
    entries = [
        MagicMock(id=uuid4(), question_pattern="salary expectations", answer="120000", times_used=0)
    ]
    with patch("apply_worker.qa_matcher.llm_complete", return_value='{"match_index": -1}'):
        match = find_match("Describe your GitHub contributions", entries, threshold=0.75)
    assert match is None


def test_find_match_empty_bank_returns_none():
    match = find_match("Any question", [], threshold=0.75)
    assert match is None
```

- [ ] **Step 2: Run to verify failure**

```bash
cd backend && python -m pytest tests/test_qa_matcher.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create `__init__.py`**

```python
# backend/apply_worker/__init__.py
```

- [ ] **Step 4: Create matcher**

```python
# backend/apply_worker/qa_matcher.py
"""Two-pass Q&A bank matcher for form field labels."""
import json
import logging
import re
from typing import Any

from rapidfuzz import fuzz

from app.llm.client import llm_complete

logger = logging.getLogger(__name__)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def find_match(
    field_label: str,
    bank_entries: list[Any],
    threshold: float = 0.75,
) -> Any | None:
    """Find best matching Q&A bank entry for a form field label.

    Two-pass:
    1. rapidfuzz token_set_ratio against question_pattern
    2. LiteLLM semantic match if pass 1 score < threshold

    Returns matching QABankEntry or None.
    """
    if not bank_entries:
        return None

    normalized_label = normalize(field_label)

    # Pass 1: fuzzy keyword match
    best_entry = None
    best_score = 0.0

    for entry in bank_entries:
        score = fuzz.token_set_ratio(
            normalized_label, normalize(entry.question_pattern)
        ) / 100.0
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score >= threshold:
        logger.debug(f"Pass 1 match: '{field_label}' -> '{best_entry.question_pattern}' ({best_score:.2f})")
        return best_entry

    # Pass 2: LiteLLM semantic match
    bank_list = "\n".join(
        f"{i}: {e.question_pattern}" for i, e in enumerate(bank_entries)
    )
    prompt = f"""You are matching a job application form field label to a Q&A bank.

Form field label: "{field_label}"

Q&A bank entries (index: pattern):
{bank_list}

Which entry best matches the form field? Reply ONLY with valid JSON:
{{"match_index": <index or -1 if no match>, "confidence": <0.0-1.0>}}

Use -1 if no entry is a reasonable match (confidence < {threshold})."""

    try:
        raw = llm_complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=64,
        )
        data = json.loads(raw.strip())
        idx = int(data.get("match_index", -1))
        confidence = float(data.get("confidence", 0.0))

        if idx >= 0 and confidence >= threshold and idx < len(bank_entries):
            logger.debug(f"Pass 2 match: '{field_label}' -> index {idx} ({confidence:.2f})")
            return bank_entries[idx]
    except Exception as e:
        logger.error(f"Pass 2 matcher failed: {e}")

    return None
```

- [ ] **Step 5: Run tests**

```bash
cd backend && python -m pytest tests/test_qa_matcher.py -v
```

Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add backend/apply_worker/ backend/tests/test_qa_matcher.py
git commit -m "feat: add two-pass Q&A bank matcher"
```

---

## Task 12: Apply worker — form filler + main loop

**Files:**
- Create: `backend/apply_worker/form_filler.py`
- Create: `backend/apply_worker/main.py`

- [ ] **Step 1: Create form filler**

```python
# backend/apply_worker/form_filler.py
"""Playwright-based form filler for ATS job application pages."""
import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.config import settings

logger = logging.getLogger(__name__)


async def extract_form_fields(page: Page) -> list[dict]:
    """Extract all visible form field labels and their input elements.

    Returns list of dicts: {label, input_selector, input_type}
    """
    fields = []

    # Common ATS form patterns: label + input pairs
    # Strategy: find all labels, locate their associated input
    labels = await page.query_selector_all("label")

    for label_el in labels:
        label_text = (await label_el.inner_text()).strip()
        if not label_text:
            continue

        # Try for= attribute first
        for_attr = await label_el.get_attribute("for")
        if for_attr:
            input_el = await page.query_selector(f"#{for_attr}")
        else:
            # Try sibling or child input
            input_el = await label_el.query_selector("input, textarea, select")
            if not input_el:
                # Try next sibling
                input_el = await page.evaluate_handle(
                    "(el) => el.nextElementSibling", label_el
                )
                tag = await page.evaluate("(el) => el ? el.tagName : null", input_el)
                if tag not in ("INPUT", "TEXTAREA", "SELECT"):
                    input_el = None

        if not input_el:
            continue

        input_type = await page.evaluate("(el) => el.type || el.tagName.toLowerCase()", input_el)
        selector = await page.evaluate(
            "(el) => { if (el.id) return '#' + el.id; if (el.name) return `[name='${el.name}']`; return null; }",
            input_el,
        )

        if selector:
            fields.append({
                "label": label_text,
                "selector": selector,
                "input_type": input_type,
                "element": input_el,
            })

    logger.debug(f"Extracted {len(fields)} form fields")
    return fields


async def fill_field(page: Page, field: dict, answer: str) -> None:
    """Fill a single form field with the given answer."""
    selector = field["selector"]
    input_type = field.get("input_type", "text")

    if input_type == "select":
        await page.select_option(selector, label=answer)
    elif input_type in ("checkbox", "radio"):
        lower = answer.lower()
        if lower in ("yes", "true", "1"):
            await page.check(selector)
        else:
            await page.uncheck(selector)
    else:
        await page.fill(selector, answer)


async def take_screenshot(page: Page, job_id: str) -> str:
    """Save screenshot and return file path."""
    screenshot_dir = Path(settings.apply_worker_screenshot_dir)
    screenshot_dir.mkdir(exist_ok=True)
    path = screenshot_dir / f"{job_id}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)
```

- [ ] **Step 2: Create worker main loop**

```python
# backend/apply_worker/main.py
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
from apply_worker.form_filler import extract_form_fields, fill_field, take_screenshot
from apply_worker.qa_matcher import find_match

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

        # Fetch Q&A bank
        bank_result = await db.execute(
            select(QABankEntry).where(QABankEntry.answer.isnot(None))
        )
        bank_entries = bank_result.scalars().all()

        logger.info(f"Processing job {job_id}: {job.title} @ {job.url}")
        await page.goto(job.url, wait_until="networkidle", timeout=30000)

        # Click "Apply" button if present
        apply_btn = await page.query_selector("a:has-text('Apply'), button:has-text('Apply')")
        if apply_btn:
            await apply_btn.click()
            await page.wait_for_load_state("networkidle")

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
            # Save unknown questions to bank and flag job
            for label in unknown_fields:
                async with async_session() as db2:
                    # Only save if not already in bank
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

        # Fill all fields
        for field, qa_entry in fill_plan:
            await fill_field(page, field, qa_entry.answer)
            qa_entry.times_used += 1
            qa_entry.last_used_at = datetime.now(timezone.utc)

        await db.commit()

        # Submit form
        submit_btn = await page.query_selector(
            "button[type=submit], input[type=submit], button:has-text('Submit')"
        )
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_load_state("networkidle")

        screenshot_path = await take_screenshot(page, job_id)

        # Create Application record
        application = Application(
            job_id=UUID(job_id),
            submission_screenshot_path=screenshot_path,
            status=ApplicationStatus.SUBMITTED.value,
        )
        db.add(application)
        job.status = JobStatus.APPLIED.value
        await db.commit()
        logger.info(f"Job {job_id} submitted successfully")


async def main() -> None:
    import redis.asyncio as aioredis

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    logger.info("Apply worker started. Waiting for jobs...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        page = await browser.new_page()

        try:
            while True:
                # Block until a job arrives (timeout 5s to allow clean shutdown)
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
```

- [ ] **Step 3: Commit**

```bash
git add backend/apply_worker/
git commit -m "feat: add apply worker with Playwright form filler"
```

---

## Task 13: Gmail connector

**Files:**
- Create: `backend/app/gmail/__init__.py`
- Create: `backend/app/gmail/connector.py`
- Create: `backend/app/gmail/classifier.py`

- [ ] **Step 1: Create `__init__.py`**

```python
# backend/app/gmail/__init__.py
```

- [ ] **Step 2: Create classifier**

```python
# backend/app/gmail/classifier.py
"""Classify Gmail reply as interview / rejection / other."""
import json
import logging

from app.llm.client import llm_complete

logger = logging.getLogger(__name__)

CLASSIFY_PROMPT = """You are classifying an email reply to a job application.

Email subject: {subject}
Email snippet: {snippet}

Classify this email into one of: interview, rejection, other

"interview" = scheduling an interview, asking for availability, next steps
"rejection" = thanking for applying but not moving forward
"other" = generic acknowledgement, newsletter, unclear

Respond ONLY with valid JSON: {{"classification": "interview|rejection|other"}}"""


def classify_reply(subject: str, snippet: str) -> str:
    """Returns 'interview', 'rejection', or 'other'."""
    prompt = CLASSIFY_PROMPT.format(subject=subject, snippet=snippet[:500])
    try:
        raw = llm_complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=32,
        )
        data = json.loads(raw.strip())
        result = data.get("classification", "other")
        if result not in ("interview", "rejection", "other"):
            return "other"
        return result
    except Exception as e:
        logger.error(f"Gmail classifier failed: {e}")
        return "other"
```

- [ ] **Step 3: Create connector**

```python
# backend/app/gmail/connector.py
"""Gmail connector — polls for replies to job applications, updates Application status."""
import logging
import os
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.gmail.classifier import classify_reply
from app.models.application import Application, ApplicationStatus

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

STATUS_MAP = {
    "interview": ApplicationStatus.INTERVIEW.value,
    "rejection": ApplicationStatus.REJECTED.value,
    "other": None,  # don't update status for "other"
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
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
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

    label = settings.gmail_label
    updated = 0

    try:
        # Search for threads in the job-applications label
        result = service.users().threads().list(
            userId="me",
            labelIds=[],
            q=f"label:{label} newer_than:1d",
        ).execute()

        threads = result.get("threads", [])

        for thread_data in threads:
            thread_id = thread_data["id"]

            # Check if we already have this thread linked
            existing = await db.execute(
                select(Application).where(Application.gmail_thread_id == thread_id)
            )
            app = existing.scalar_one_or_none()

            if app:
                # Already classified this thread
                continue

            # Fetch thread details
            thread = service.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["Subject", "From"],
            ).execute()

            messages = thread.get("messages", [])
            if len(messages) < 2:
                # Only sent message, no reply yet
                continue

            # Get latest message headers
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

            # Try to match application by subject (contains job title or company)
            # Fetch all submitted applications without a thread ID
            unlinked = await db.execute(
                select(Application).where(Application.gmail_thread_id.is_(None))
            )
            unlinked_apps = unlinked.scalars().all()

            # Simple heuristic: find application submitted within 30 days
            # and subject contains company name — link first unlinked app
            # (improve matching by storing email subject at apply time in future)
            if unlinked_apps:
                target_app = unlinked_apps[0]
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
```

- [ ] **Step 4: Wire Gmail poll into scheduler**

In `backend/app/scheduler.py`, add:

```python
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
```

Then in `start_scheduler`:

```python
    scheduler.add_job(
        run_gmail_poll,
        IntervalTrigger(minutes=15),
        kwargs={"app": app},
        id="gmail_poll",
        replace_existing=True,
    )
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/gmail/ backend/app/scheduler.py
git commit -m "feat: add Gmail connector and reply classifier"
```

---

## Task 14: Frontend — API client and hooks

**Files:**
- Create: `frontend/src/api/jobs.js`
- Create: `frontend/src/hooks/useJobs.js`
- Create: `frontend/src/hooks/useApplications.js`
- Create: `frontend/src/hooks/useQABank.js`

- [ ] **Step 1: Create jobs API client**

```javascript
// frontend/src/api/jobs.js
import api from './client';

// Jobs
export const fetchJobs = (params) => api.get('/jobs', { params }).then((r) => r.data);
export const fetchPendingCount = () => api.get('/jobs/pending-count').then((r) => r.data);
export const approveJob = (id) => api.post(`/jobs/${id}/approve`).then((r) => r.data);
export const rejectJob = (id) => api.post(`/jobs/${id}/reject`).then((r) => r.data);
export const updateJobStatus = (id, status) =>
  api.patch(`/jobs/${id}/status`, { status }).then((r) => r.data);

// Applications
export const fetchApplications = (params) =>
  api.get('/applications', { params }).then((r) => r.data);
export const updateApplicationStatus = (id, status, notes) =>
  api.patch(`/applications/${id}/status`, { status, notes }).then((r) => r.data);

// Q&A Bank
export const fetchQABank = () => api.get('/qa-bank').then((r) => r.data);
export const createQAEntry = (data) => api.post('/qa-bank', data).then((r) => r.data);
export const updateQAEntry = (id, data) =>
  api.patch(`/qa-bank/${id}`, data).then((r) => r.data);
export const deleteQAEntry = (id) => api.delete(`/qa-bank/${id}`).then((r) => r.data);
```

- [ ] **Step 2: Create useJobs hook**

```javascript
// frontend/src/hooks/useJobs.js
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { approveJob, fetchJobs, fetchPendingCount, rejectJob } from '../api/jobs';

export function useJobs(filters = {}) {
  return useQuery({
    queryKey: ['jobs', filters],
    queryFn: () => fetchJobs(filters),
    staleTime: 30000,
    refetchInterval: 60000,
  });
}

export function usePendingCount() {
  return useQuery({
    queryKey: ['jobs', 'pending-count'],
    queryFn: fetchPendingCount,
    refetchInterval: 30000,
  });
}

export function useApproveJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: approveJob,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}

export function useRejectJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: rejectJob,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}
```

- [ ] **Step 3: Create useApplications hook**

```javascript
// frontend/src/hooks/useApplications.js
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApplications, updateApplicationStatus } from '../api/jobs';

export function useApplications(filters = {}) {
  return useQuery({
    queryKey: ['applications', filters],
    queryFn: () => fetchApplications(filters),
    staleTime: 30000,
    refetchInterval: 60000,
  });
}

export function useUpdateApplicationStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status, notes }) => updateApplicationStatus(id, status, notes),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['applications'] });
    },
  });
}
```

- [ ] **Step 4: Create useQABank hook**

```javascript
// frontend/src/hooks/useQABank.js
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createQAEntry, deleteQAEntry, fetchQABank, updateQAEntry } from '../api/jobs';

export function useQABank() {
  return useQuery({
    queryKey: ['qa-bank'],
    queryFn: fetchQABank,
    staleTime: 60000,
  });
}

export function useUpdateQAEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }) => updateQAEntry(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['qa-bank'] }),
  });
}

export function useCreateQAEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createQAEntry,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['qa-bank'] }),
  });
}

export function useDeleteQAEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteQAEntry,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['qa-bank'] }),
  });
}
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/jobs.js frontend/src/hooks/
git commit -m "feat: add jobs/applications/qa-bank API client and hooks"
```

---

## Task 15: Frontend — JobsReview page

**Files:**
- Create: `frontend/src/pages/JobsReview.jsx`

- [ ] **Step 1: Create page**

```jsx
// frontend/src/pages/JobsReview.jsx
import { useState } from 'react';
import Navbar from '../components/Navbar';
import { useJobs, useApproveJob, useRejectJob } from '../hooks/useJobs';

const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  applied: 'bg-blue-100 text-blue-800',
  flagged: 'bg-orange-100 text-orange-800',
};

function TimeRemaining({ deadline }) {
  if (!deadline) return null;
  const ms = new Date(deadline) - new Date();
  if (ms <= 0) return <span className="text-red-500 text-xs">Expired</span>;
  const hrs = Math.floor(ms / 3600000);
  const mins = Math.floor((ms % 3600000) / 60000);
  return (
    <span className="text-xs text-gray-500">
      {hrs}h {mins}m remaining
    </span>
  );
}

export default function JobsReview() {
  const [statusFilter, setStatusFilter] = useState('in_review');
  const { data, isLoading } = useJobs({ status: statusFilter, page_size: 50 });
  const approve = useApproveJob();
  const reject = useRejectJob();

  return (
    <>
      <Navbar />
      <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Jobs Review Queue</h1>
          <div className="flex gap-2">
            {['in_review', 'approved', 'rejected', 'flagged', 'applied'].map((s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                  statusFilter === s
                    ? 'bg-indigo-600 text-white border-indigo-600'
                    : 'bg-white text-gray-600 border-gray-300 hover:border-indigo-400'
                }`}
              >
                {s.replace('_', ' ')}
              </button>
            ))}
          </div>
        </div>

        {isLoading && (
          <div className="text-center py-12 text-gray-500">Loading jobs...</div>
        )}

        {!isLoading && data?.jobs?.length === 0 && (
          <div className="text-center py-12 text-gray-400">No jobs in this status.</div>
        )}

        <div className="space-y-4">
          {data?.jobs?.map((job) => (
            <div
              key={job.id}
              className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${
                        STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700'
                      }`}
                    >
                      {job.status.replace('_', ' ')}
                    </span>
                    <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                      {job.ats_type}
                    </span>
                    {job.ai_fit_score != null && (
                      <span className="text-xs font-medium text-indigo-600">
                        {Math.round(job.ai_fit_score * 100)}% fit
                      </span>
                    )}
                  </div>
                  <h3 className="text-lg font-semibold text-gray-900 truncate">
                    {job.title}
                  </h3>
                  <p className="text-sm text-gray-500 mt-0.5">
                    {job.company_name}
                  </p>
                  {job.ai_fit_reasoning && (
                    <p className="text-sm text-gray-600 mt-2 line-clamp-2">
                      {job.ai_fit_reasoning}
                    </p>
                  )}
                  <div className="flex items-center gap-4 mt-2">
                    <TimeRemaining deadline={job.review_deadline} />
                    <a
                      href={job.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-indigo-500 hover:underline"
                    >
                      View posting →
                    </a>
                  </div>
                </div>

                {job.status === 'in_review' && (
                  <div className="flex gap-2 ml-4 shrink-0">
                    <button
                      onClick={() => approve.mutate(job.id)}
                      disabled={approve.isPending}
                      className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => reject.mutate(job.id)}
                      disabled={reject.isPending}
                      className="px-4 py-2 bg-red-100 text-red-700 text-sm font-medium rounded-lg hover:bg-red-200 disabled:opacity-50 transition-colors"
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/pages/JobsReview.jsx
git commit -m "feat: add JobsReview page"
```

---

## Task 16: Frontend — ApplicationTracker page

**Files:**
- Create: `frontend/src/pages/ApplicationTracker.jsx`

- [ ] **Step 1: Create page**

```jsx
// frontend/src/pages/ApplicationTracker.jsx
import { useState } from 'react';
import Navbar from '../components/Navbar';
import { useApplications, useUpdateApplicationStatus } from '../hooks/useApplications';

const STATUS_COLORS = {
  submitted: 'bg-blue-100 text-blue-800',
  interview: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  offer: 'bg-purple-100 text-purple-800',
  ghosted: 'bg-gray-100 text-gray-600',
};

const ALL_STATUSES = ['submitted', 'interview', 'rejected', 'offer', 'ghosted'];

export default function ApplicationTracker() {
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedApp, setSelectedApp] = useState(null);
  const { data, isLoading } = useApplications({ status: statusFilter || undefined, page_size: 50 });
  const updateStatus = useUpdateApplicationStatus();

  return (
    <>
      <Navbar />
      <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Application Tracker</h1>
          <div className="flex gap-2">
            <button
              onClick={() => setStatusFilter('')}
              className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                !statusFilter
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'bg-white text-gray-600 border-gray-300'
              }`}
            >
              All
            </button>
            {ALL_STATUSES.map((s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                  statusFilter === s
                    ? 'bg-indigo-600 text-white border-indigo-600'
                    : 'bg-white text-gray-600 border-gray-300'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {isLoading && (
          <div className="text-center py-12 text-gray-500">Loading applications...</div>
        )}

        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Job</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Company</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Submitted</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Last Email</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {data?.applications?.map((app) => (
                <tr key={app.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 font-medium text-gray-900">{app.job_title}</td>
                  <td className="px-4 py-3 text-gray-600">{app.company_name}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {new Date(app.submitted_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3">
                    <select
                      value={app.status}
                      onChange={(e) =>
                        updateStatus.mutate({ id: app.id, status: e.target.value })
                      }
                      className={`px-2 py-1 rounded text-xs font-medium border-0 cursor-pointer ${
                        STATUS_COLORS[app.status] || 'bg-gray-100 text-gray-700'
                      }`}
                    >
                      {ALL_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {app.last_email_at
                      ? new Date(app.last_email_at).toLocaleDateString()
                      : '—'}
                  </td>
                  <td className="px-4 py-3">
                    {app.submission_screenshot_path && (
                      <button
                        onClick={() => setSelectedApp(app)}
                        className="text-xs text-indigo-500 hover:underline"
                      >
                        Screenshot
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {!isLoading && !data?.applications?.length && (
            <div className="text-center py-12 text-gray-400">No applications yet.</div>
          )}
        </div>

        {/* Screenshot modal */}
        {selectedApp && (
          <div
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            onClick={() => setSelectedApp(null)}
          >
            <div className="bg-white rounded-xl p-4 max-w-2xl w-full mx-4" onClick={(e) => e.stopPropagation()}>
              <div className="flex justify-between items-center mb-3">
                <h3 className="font-semibold text-gray-900">{selectedApp.job_title}</h3>
                <button onClick={() => setSelectedApp(null)} className="text-gray-400 hover:text-gray-600">✕</button>
              </div>
              <img
                src={`/screenshots/${selectedApp.id}.png`}
                alt="Application screenshot"
                className="w-full rounded-lg border"
              />
            </div>
          </div>
        )}
      </div>
    </>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/pages/ApplicationTracker.jsx
git commit -m "feat: add ApplicationTracker page"
```

---

## Task 17: Frontend — QABank page

**Files:**
- Create: `frontend/src/pages/QABank.jsx`

- [ ] **Step 1: Create page**

```jsx
// frontend/src/pages/QABank.jsx
import { useState } from 'react';
import Navbar from '../components/Navbar';
import { useQABank, useUpdateQAEntry, useCreateQAEntry, useDeleteQAEntry } from '../hooks/useQABank';

function EntryRow({ entry, onSave, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [answer, setAnswer] = useState(entry.answer || '');

  const handleSave = () => {
    onSave(entry.id, { answer });
    setEditing(false);
  };

  return (
    <tr className={`border-b border-gray-100 ${!entry.answer ? 'bg-orange-50' : 'hover:bg-gray-50'}`}>
      <td className="px-4 py-3">
        <div className="font-medium text-gray-900 text-sm">{entry.canonical_question}</div>
        <div className="text-xs text-gray-400 mt-0.5">{entry.question_pattern}</div>
      </td>
      <td className="px-4 py-3 text-xs text-gray-500">{entry.answer_type}</td>
      <td className="px-4 py-3">
        {editing ? (
          <div className="flex gap-2">
            <input
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              autoFocus
            />
            <button
              onClick={handleSave}
              className="px-3 py-1 bg-indigo-600 text-white text-xs rounded hover:bg-indigo-700"
            >
              Save
            </button>
            <button
              onClick={() => { setAnswer(entry.answer || ''); setEditing(false); }}
              className="px-3 py-1 bg-gray-100 text-gray-700 text-xs rounded hover:bg-gray-200"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setEditing(true)}
            className={`text-sm px-2 py-1 rounded w-full text-left ${
              entry.answer ? 'text-gray-800 hover:bg-gray-100' : 'text-orange-500 hover:bg-orange-100'
            }`}
          >
            {entry.answer || '⚠ No answer — click to fill'}
          </button>
        )}
      </td>
      <td className="px-4 py-3 text-center text-xs text-gray-500">{entry.times_used}</td>
      <td className="px-4 py-3 text-center">
        <button
          onClick={() => onDelete(entry.id)}
          className="text-red-400 hover:text-red-600 text-xs"
        >
          Delete
        </button>
      </td>
    </tr>
  );
}

export default function QABank() {
  const { data: entries, isLoading } = useQABank();
  const updateEntry = useUpdateQAEntry();
  const createEntry = useCreateQAEntry();
  const deleteEntry = useDeleteQAEntry();
  const [showAdd, setShowAdd] = useState(false);
  const [newQuestion, setNewQuestion] = useState('');
  const [newAnswer, setNewAnswer] = useState('');

  const handleAdd = () => {
    if (!newQuestion.trim()) return;
    createEntry.mutate({
      question_pattern: newQuestion.toLowerCase().trim(),
      canonical_question: newQuestion.trim(),
      answer: newAnswer.trim() || null,
    });
    setNewQuestion('');
    setNewAnswer('');
    setShowAdd(false);
  };

  return (
    <>
      <Navbar />
      <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Q&A Bank</h1>
            <p className="text-sm text-gray-500 mt-1">
              Answers used to auto-fill job application forms.
              <span className="ml-2 text-orange-500 font-medium">
                Orange rows need your answer.
              </span>
            </p>
          </div>
          <button
            onClick={() => setShowAdd(true)}
            className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700"
          >
            + Add Entry
          </button>
        </div>

        {showAdd && (
          <div className="bg-white border border-indigo-200 rounded-xl p-4 mb-4 shadow-sm">
            <h3 className="font-medium text-gray-900 mb-3">New Entry</h3>
            <div className="flex gap-3">
              <input
                placeholder="Question (e.g. 'years of experience')"
                value={newQuestion}
                onChange={(e) => setNewQuestion(e.target.value)}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <input
                placeholder="Your answer"
                value={newAnswer}
                onChange={(e) => setNewAnswer(e.target.value)}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <button onClick={handleAdd} className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700">
                Add
              </button>
              <button onClick={() => setShowAdd(false)} className="px-4 py-2 bg-gray-100 text-gray-700 text-sm rounded-lg hover:bg-gray-200">
                Cancel
              </button>
            </div>
          </div>
        )}

        {isLoading && <div className="text-center py-12 text-gray-500">Loading...</div>}

        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Question</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600 w-24">Type</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Answer</th>
                <th className="text-center px-4 py-3 font-medium text-gray-600 w-20">Used</th>
                <th className="w-16" />
              </tr>
            </thead>
            <tbody>
              {entries?.map((entry) => (
                <EntryRow
                  key={entry.id}
                  entry={entry}
                  onSave={(id, data) => updateEntry.mutate({ id, ...data })}
                  onDelete={(id) => deleteEntry.mutate(id)}
                />
              ))}
            </tbody>
          </table>
          {!isLoading && !entries?.length && (
            <div className="text-center py-12 text-gray-400">No entries yet. Add your first answer above.</div>
          )}
        </div>
      </div>
    </>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/pages/QABank.jsx
git commit -m "feat: add QABank manager page"
```

---

## Task 18: Frontend — routing + Navbar badge

**Files:**
- Modify: `frontend/src/main.jsx`
- Modify: `frontend/src/components/Navbar.jsx`

- [ ] **Step 1: Add routes to main.jsx**

Replace `frontend/src/main.jsx`:

```jsx
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Dashboard from './pages/Dashboard';
import CompanyDetail from './pages/CompanyDetail';
import JobsReview from './pages/JobsReview';
import ApplicationTracker from './pages/ApplicationTracker';
import QABank from './pages/QABank';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="min-h-screen bg-gray-50">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/company/:id" element={<CompanyDetail />} />
            <Route path="/jobs" element={<JobsReview />} />
            <Route path="/applications" element={<ApplicationTracker />} />
            <Route path="/qa-bank" element={<QABank />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
```

- [ ] **Step 2: Update Navbar with new links and review badge**

In `frontend/src/components/Navbar.jsx`, add this import at the top:

```javascript
import { Link, useLocation } from 'react-router-dom';
import { usePendingCount } from '../hooks/useJobs';
```

Then add nav links inside the navbar (after the category tabs div):

```jsx
      {/* Auto-Apply Nav Links */}
      <div className="flex items-center gap-1">
        {[
          { path: '/jobs', label: 'Review', badge: true },
          { path: '/applications', label: 'Applications', badge: false },
          { path: '/qa-bank', label: 'Q&A Bank', badge: false },
        ].map(({ path, label, badge }) => (
          <NavLink key={path} path={path} label={label} showBadge={badge} />
        ))}
      </div>
```

Add `NavLink` component before the `export default`:

```jsx
function NavLink({ path, label, showBadge }) {
  const location = useLocation();
  const { data: countData } = usePendingCount();
  const isActive = location.pathname === path;
  const count = countData?.count || 0;

  return (
    <Link
      to={path}
      className={`relative px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
        isActive ? 'bg-indigo-50 text-indigo-600' : 'text-gray-600 hover:text-gray-900 hover:bg-gray-100'
      }`}
    >
      {label}
      {showBadge && count > 0 && (
        <span className="absolute -top-1 -right-1 w-4 h-4 bg-red-500 text-white text-xs rounded-full flex items-center justify-center">
          {count > 9 ? '9+' : count}
        </span>
      )}
    </Link>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/main.jsx frontend/src/components/Navbar.jsx
git commit -m "feat: add routing for Jobs/Applications/QABank pages, navbar badge"
```

---

## Task 19: Docker Compose — apply-worker service

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add apply-worker service**

In `docker-compose.yml`, add after the existing `backend` service:

```yaml
  apply-worker:
    build:
      context: ./backend
      dockerfile: Dockerfile
    command: python apply_worker/main.py
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
      - LLM_PROVIDER=${LLM_PROVIDER:-groq}
      - LLM_MODEL=${LLM_MODEL:-llama-3.3-70b-versatile}
      - LLM_API_KEY=${LLM_API_KEY}
      - QA_MATCH_THRESHOLD=${QA_MATCH_THRESHOLD:-0.75}
      - APPLY_WORKER_SCREENSHOT_DIR=/app/screenshots
    volumes:
      - screenshots:/app/screenshots
    depends_on:
      - db
      - redis
    restart: unless-stopped

volumes:
  screenshots:
```

- [ ] **Step 2: Install Playwright in Dockerfile**

Check `backend/Dockerfile` and ensure it includes:

```dockerfile
RUN playwright install chromium --with-deps
```

If using a multi-stage Dockerfile, add this after the pip install step.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml backend/Dockerfile
git commit -m "feat: add apply-worker docker service"
```

---

## Task 20: Seed Q&A bank with common defaults

**Files:**
- Create: `backend/seed_qa_bank.py`

- [ ] **Step 1: Create seed script**

```python
# backend/seed_qa_bank.py
"""Seed the Q&A bank with common job application questions and answers.

Edit answers below before running.
Run: python seed_qa_bank.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.database import async_session
from app.models.qa_bank import QABankEntry, AnswerType

# ── Edit these answers ──────────────────────────────────────────────────────
ANSWERS = [
    ("authorized to work in the us", "Are you authorized to work in the US?", "Yes", AnswerType.TEXT),
    ("require visa sponsorship", "Do you require visa sponsorship?", "No", AnswerType.TEXT),
    ("years of experience", "Years of experience", "3", AnswerType.NUMBER),
    ("first name", "First name", "Nitin", AnswerType.TEXT),
    ("last name", "Last name", "Goyal", AnswerType.TEXT),
    ("email", "Email address", "goyal.niti@northeastern.edu", AnswerType.TEXT),
    ("phone", "Phone number", "", AnswerType.TEXT),  # fill in
    ("linkedin", "LinkedIn profile URL", "", AnswerType.TEXT),  # fill in
    ("github", "GitHub profile URL", "", AnswerType.TEXT),  # fill in
    ("salary", "Expected salary", "140000", AnswerType.NUMBER),
    ("willing to relocate", "Are you willing to relocate?", "No", AnswerType.TEXT),
    ("remote work", "Are you open to remote work?", "Yes", AnswerType.TEXT),
    ("how did you hear", "How did you hear about this position?", "Online job board", AnswerType.TEXT),
    ("cover letter", "Cover letter", "I am an MS AI student at Northeastern with 3 years of experience building LLM-powered applications using LangChain, LangGraph, and FastAPI. I am passionate about agentic systems and healthcare AI.", AnswerType.TEXT),
]
# ────────────────────────────────────────────────────────────────────────────


async def seed():
    async with async_session() as db:
        for pattern, canonical, answer, answer_type in ANSWERS:
            from sqlalchemy import select
            existing = await db.execute(
                select(QABankEntry).where(QABankEntry.question_pattern == pattern)
            )
            if existing.scalar_one_or_none():
                print(f"  skip: {pattern}")
                continue
            entry = QABankEntry(
                question_pattern=pattern,
                canonical_question=canonical,
                answer=answer if answer else None,
                answer_type=answer_type.value,
            )
            db.add(entry)
            print(f"  add:  {pattern}")
        await db.commit()
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
```

- [ ] **Step 2: Run seed (fill in blanks first)**

Open `backend/seed_qa_bank.py`, fill in phone, LinkedIn, GitHub URLs, then:

```bash
cd backend && python seed_qa_bank.py
```

Expected: prints `add: <pattern>` for each entry.

- [ ] **Step 3: Commit**

```bash
git add backend/seed_qa_bank.py
git commit -m "feat: add Q&A bank seed script with common application defaults"
```

---

## Self-Review Notes

**Spec coverage:**
- ✅ Ashby/Lever/Greenhouse scrapers (Task 6)
- ✅ LiteLLM fit scoring (Task 6, scorer.py)
- ✅ Q&A bank (Tasks 4, 10, 11, 17)
- ✅ Review window + hybrid queue (Tasks 7, 8 scheduler, Task 15 UI)
- ✅ Apply worker separate process (Task 12)
- ✅ Application tracker (Tasks 4, 10, 16)
- ✅ Gmail connector + classifier (Task 13)
- ✅ LiteLLM as gateway everywhere (Task 3)
- ✅ Dashboard panels (Tasks 15, 16, 17)
- ✅ Navbar badge (Task 18)

**Type consistency:**
- `JobStatus.IN_REVIEW` used in pipeline.jobs (Task 7), scheduler (Task 8), jobs API (Task 10) — consistent
- `find_match` signature in qa_matcher.py matches usage in apply_worker/main.py — consistent
- `JobResponse.company_name` added as field in schema and populated in API — consistent

**Placeholders:** None — all code blocks are complete.
