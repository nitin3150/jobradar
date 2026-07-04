# Phase 1 — Wire the Job→Apply Loop End-to-End — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the already-built-but-disconnected career-ops pieces (routers, resume text, preferences, dry-run apply) so a job flows fetch → score → review → auto-apply end-to-end.

**Architecture:** Minimal wire-through (Approach A). Mount 6 dead routers, add missing `Settings` fields, extract resume text at upload (cached to a new column), derive the scorer's candidate profile from the default resume + DB preferences, make the job pipeline read DB preferences, and add a dry-run apply path for safe verification. Each DB-touching helper is split into a pure function (unit-tested) + a thin async DB wrapper.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, pytest/pytest-asyncio, pypdf, python-docx.

**Working branch:** `phase1-job-loop-wiring` (already checked out).

**Spec:** `docs/superpowers/specs/2026-07-04-job-loop-wiring-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `app/config.py` | Add missing `Settings` fields | Modify |
| `app/api/router.py` | Mount the 6 dead routers | Modify |
| `app/main.py` | Re-enable `/screenshots` StaticFiles | Modify |
| `app/resumes/__init__.py` | New package | Create |
| `app/resumes/extract.py` | PDF/docx/txt → text | Create |
| `app/resumes/profile.py` | Compose candidate profile from resume + prefs | Create |
| `app/models/resume.py` | Add `extracted_text` column | Modify |
| `alembic/versions/007_add_resume_extracted_text.py` | Migration | Create |
| `app/api/resumes.py` | Extract text at upload | Modify |
| `app/scrapers/jobs/scorer.py` | `score_job(..., profile)` signature | Modify |
| `app/pipeline/jobs.py` | Build profile once/run; read DB prefs | Modify |
| `apply_worker/main.py` | Dry-run apply path | Modify |
| `tests/test_api/test_router_mounts.py` | Route-registration tests | Create |
| `tests/test_resumes/test_extract.py` | Extraction unit tests | Create |
| `tests/test_resumes/test_profile.py` | Profile compose unit tests | Create |
| `tests/test_pipeline/test_prefs_resolver.py` | Prefs resolver unit tests | Create |

---

## Task 1: Add missing Settings fields

**Files:**
- Modify: `app/config.py`
- Test: `tests/test_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
def test_missing_api_keys_default_to_none():
    from app.config import Settings

    s = Settings()
    # These were referenced in code but never defined -> AttributeError at runtime.
    assert s.serper_api_key is None
    assert s.apify_api_key is None
    assert s.gmail_token_path is None
    assert s.gmail_credentials_path is None
    assert s.gmail_label is None


def test_apply_dry_run_defaults_false():
    from app.config import Settings

    assert Settings().apply_dry_run is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'serper_api_key'`

- [ ] **Step 3: Add the fields**

In `app/config.py`, after the `discovery_max_results` line (currently `:102`) and before `settings = Settings()`, add:

```python
    # --- External API keys referenced in code but previously undefined ---
    # serper.dev SERP API (app/scrapers/jobs/search.py discovery path)
    serper_api_key: str | None = None
    # Apify actor key (enrichment.py Twitter signals, twitter_scraper.py)
    apify_api_key: str | None = None

    # Gmail poll (app/gmail/connector.py) — readonly poll only
    gmail_token_path: str | None = None
    gmail_credentials_path: str | None = None
    gmail_label: str | None = None

    # Apply worker dry-run: fill + attach + screenshot, but never click submit
    apply_dry_run: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add serper/apify/gmail/dry-run Settings fields"
```

---

## Task 2: Mount the 6 dead routers + StaticFiles

**Files:**
- Modify: `app/api/router.py:13-20`
- Modify: `app/main.py:1-9,77-84`
- Test: `tests/test_api/test_router_mounts.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_api/test_router_mounts.py`:

```python
import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/api/settings",
        "/api/resumes",
        "/api/jobs",
        "/api/applications",
        "/api/qa-bank",
        "/api/outreach/generate",
    ],
)
def test_router_mounted(path):
    from app.main import app

    paths = list(app.openapi().get("paths", {}).keys())
    assert any(p == path or p.startswith(path) for p in paths), (path, paths)
```

Note: exact sub-paths vary per router; the `startswith` check tolerates prefixes like `/api/jobs/{job_id}`. Adjust a path only if a router uses a different prefix than its filename suggests — verify by reading each router's `APIRouter(prefix=...)` if a case fails.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api/test_router_mounts.py -v`
Expected: FAIL for `/api/settings`, `/api/resumes`, `/api/jobs`, `/api/applications`, `/api/qa-bank`, `/api/outreach/generate` (routers commented out).

- [ ] **Step 3: Uncomment the router mounts**

In `app/api/router.py`, replace lines 13-20 with:

```python
api_router.include_router(companies_router)
api_router.include_router(outreach_router)
api_router.include_router(pipeline_router)
api_router.include_router(jobs_router)
api_router.include_router(applications_router)
api_router.include_router(qa_bank_router)
api_router.include_router(resumes_router)
api_router.include_router(settings_router)
```

- [ ] **Step 4: Re-enable the screenshots StaticFiles mount**

In `app/main.py`, change the import lines 2-9 so `os`/`Path`/`StaticFiles` are imported:

```python
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
```

Then replace the commented block at lines 77-84 with:

```python
_screenshots_dir = Path(settings.apply_worker_screenshot_dir)
_screenshots_dir.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(_screenshots_dir)), name="screenshots")

# Resumes are intentionally NOT mounted as StaticFiles — downloads go through
# the /api/resumes/{id}/download endpoint with Content-Disposition: attachment.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_api/test_router_mounts.py tests/test_api/test_preferences.py -v`
Expected: PASS — including the previously-failing `test_settings_routes_registered`.

- [ ] **Step 6: Commit**

```bash
git add app/api/router.py app/main.py tests/test_api/test_router_mounts.py
git commit -m "feat: mount jobs/applications/qa-bank/resumes/settings/outreach routers + screenshots"
```

---

## Task 3: Resume text extraction module

**Files:**
- Create: `app/resumes/__init__.py`
- Create: `app/resumes/extract.py`
- Create: `tests/test_resumes/__init__.py`
- Create: `tests/test_resumes/test_extract.py`
- Modify: `pyproject.toml` (add `pypdf`, `python-docx`)

- [ ] **Step 1: Add dependencies**

Add to `pyproject.toml` under the `dependencies` array (match existing formatting):

```toml
    "pypdf>=4.0",
    "python-docx>=1.1",
```

Then install:

Run: `pip install "pypdf>=4.0" "python-docx>=1.1"`
Expected: successful install (also update the lockfile if the project uses `uv`: `uv lock` — skip if `uv` unavailable).

- [ ] **Step 2: Write the failing test**

Create `tests/test_resumes/__init__.py` (empty file).

Create `tests/test_resumes/test_extract.py`:

```python
from pathlib import Path

from app.resumes.extract import extract_text, MAX_EXTRACT_CHARS


def test_extract_txt(tmp_path: Path):
    f = tmp_path / "r.txt"
    f.write_text("Nitin — ML Engineer, LangGraph, FastAPI")
    assert "LangGraph" in extract_text(f, "text/plain")


def test_extract_docx(tmp_path: Path):
    import docx

    f = tmp_path / "r.docx"
    doc = docx.Document()
    doc.add_paragraph("Senior ML Engineer")
    doc.add_paragraph("Skills: PyTorch, FastAPI")
    doc.save(str(f))
    out = extract_text(
        f,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "PyTorch" in out and "Senior ML Engineer" in out


def test_unsupported_type_returns_empty(tmp_path: Path):
    f = tmp_path / "r.doc"
    f.write_bytes(b"\xff\xfe legacy doc")
    assert extract_text(f, "application/msword") == ""


def test_corrupt_pdf_returns_empty(tmp_path: Path):
    f = tmp_path / "bad.pdf"
    f.write_bytes(b"not really a pdf")
    # Failure must never raise — extraction is best-effort.
    assert extract_text(f, "application/pdf") == ""


def test_output_is_capped(tmp_path: Path):
    f = tmp_path / "big.txt"
    f.write_text("x" * (MAX_EXTRACT_CHARS + 5000))
    assert len(extract_text(f, "text/plain")) == MAX_EXTRACT_CHARS
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_resumes/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.resumes'`

- [ ] **Step 4: Write the implementation**

Create `app/resumes/__init__.py` (empty file).

Create `app/resumes/extract.py`:

```python
"""Best-effort resume text extraction. Never raises — failures return ""."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_EXTRACT_CHARS = 20_000

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TEXT_CTS = {"text/plain", "text/markdown"}


def extract_text(path: Path, content_type: str) -> str:
    """Extract plain text from a resume file. Returns "" on any failure or
    unsupported type (legacy .doc / application/msword is not supported)."""
    try:
        if content_type == "application/pdf":
            text = _extract_pdf(path)
        elif content_type == _DOCX_CT:
            text = _extract_docx(path)
        elif content_type in _TEXT_CTS:
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            return ""
        return text[:MAX_EXTRACT_CHARS]
    except Exception as e:
        logger.warning("Resume text extraction failed for %s: %s", path, e)
        return ""


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_resumes/test_extract.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add app/resumes/__init__.py app/resumes/extract.py tests/test_resumes/ pyproject.toml
git commit -m "feat: resume text extraction (pdf/docx/txt), best-effort"
```

---

## Task 4: Add `extracted_text` column + migration + wire into upload

**Files:**
- Modify: `app/models/resume.py`
- Create: `alembic/versions/007_add_resume_extracted_text.py`
- Modify: `app/api/resumes.py:98-163` (upload path)

- [ ] **Step 1: Add the model column**

In `app/models/resume.py`, add after the `uploaded_at` column:

```python
    # Cached plain-text extraction of the resume file. Filled at upload time
    # (see app/api/resumes.py). Nullable: extraction is best-effort.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(`Text` is already imported at the top of the file.)

- [ ] **Step 2: Create the migration**

Create `alembic/versions/007_add_resume_extracted_text.py`:

```python
"""Add resumes.extracted_text (cached resume text for scoring)."""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("extracted_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("resumes", "extracted_text")
```

- [ ] **Step 3: Run the migration**

Run: `alembic upgrade head`
Expected: `Running upgrade 006 -> 007, Add resumes.extracted_text`. (Requires the DB reachable via `database_url`. If DB is unavailable in this environment, note it and defer to the verification task; the migration is still committed.)

- [ ] **Step 4: Wire extraction into the upload endpoint**

In `app/api/resumes.py`, add the import near the top (after line 15):

```python
from app.resumes.extract import extract_text
```

Then in `upload_resume`, inside the `try:` block where `new_resume = Resume(...)` is constructed (currently `:133-140`), add `extracted_text` to the constructor. Replace the `Resume(...)` call with:

```python
        new_resume = Resume(
            name=original_name[:512],
            storage_path=storage_filename,
            content_type=content_type,
            size_bytes=len(raw),
            tags=_normalize_tags(tags, None),
            is_default=is_default,
            extracted_text=extract_text(on_disk, content_type) or None,
        )
```

- [ ] **Step 5: Verify import + model load**

Run: `python -c "from app.api.resumes import upload_resume; from app.models.resume import Resume; print('ok', hasattr(Resume, 'extracted_text'))"`
Expected: `ok True`

- [ ] **Step 6: Commit**

```bash
git add app/models/resume.py alembic/versions/007_add_resume_extracted_text.py app/api/resumes.py
git commit -m "feat: cache extracted resume text on upload (migration 007)"
```

---

## Task 5: Candidate profile builder

**Files:**
- Create: `app/resumes/profile.py`
- Create: `tests/test_resumes/test_profile.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_resumes/test_profile.py`:

```python
from app.resumes.profile import compose_profile


def test_compose_with_resume_and_roles():
    out = compose_profile("Nitin — LangGraph, FastAPI", ["AI Engineer", "ML Engineer"])
    assert "AI Engineer" in out and "ML Engineer" in out
    assert "LangGraph" in out


def test_compose_roles_only():
    out = compose_profile(None, ["LLM Engineer"])
    assert "LLM Engineer" in out


def test_compose_resume_only():
    out = compose_profile("PyTorch, Docker", [])
    assert "PyTorch" in out


def test_compose_empty_returns_blank():
    # No resume, no roles -> "" so the scorer applies its own default fallback.
    assert compose_profile(None, []) == ""
    assert compose_profile("", []) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_resumes/test_profile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.resumes.profile'`

- [ ] **Step 3: Write the implementation**

Create `app/resumes/profile.py`:

```python
"""Build the candidate profile string that drives job-fit scoring.

`compose_profile` is pure (unit-tested). `build_candidate_profile` is the thin
async DB wrapper used by the job pipeline.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import Preferences
from app.models.resume import Resume


def compose_profile(resume_text: str | None, target_roles: list[str]) -> str:
    """Compose a profile string from resume text + target roles.

    Returns "" when there is nothing to say — the scorer then falls back to its
    own default profile.
    """
    parts: list[str] = []
    if target_roles:
        parts.append("Target roles: " + ", ".join(target_roles))
    if resume_text and resume_text.strip():
        parts.append("Resume:\n" + resume_text.strip())
    return "\n\n".join(parts)


async def build_candidate_profile(db: AsyncSession) -> str:
    """Fetch the default resume text + preference roles, compose the profile."""
    res = await db.execute(select(Resume).where(Resume.is_default.is_(True)))
    resume = res.scalar_one_or_none()
    resume_text = resume.extracted_text if resume else None

    prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
    roles = list(prefs.target_roles) if prefs and prefs.target_roles else []

    return compose_profile(resume_text, roles)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_resumes/test_profile.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/resumes/profile.py tests/test_resumes/test_profile.py
git commit -m "feat: candidate profile builder (resume text + target roles)"
```

---

## Task 6: Thread profile + DB preferences into the job pipeline

**Files:**
- Modify: `app/scrapers/jobs/scorer.py`
- Modify: `app/pipeline/jobs.py`
- Create: `tests/test_pipeline/test_prefs_resolver.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline/test_prefs_resolver.py`:

```python
from types import SimpleNamespace

from app.pipeline.jobs import resolve_prefs


def _defaults():
    return SimpleNamespace(
        target_roles=["Software Engineer"],
        job_fit_threshold=0.6,
        review_window_hours=2,
    )


def test_resolve_uses_db_prefs_when_present():
    prefs = SimpleNamespace(
        target_roles=["AI Engineer"], job_fit_threshold=0.8, review_window_hours=3.0
    )
    roles, threshold, window = resolve_prefs(prefs, _defaults())
    assert roles == ["AI Engineer"]
    assert threshold == 0.8
    assert window == 3.0


def test_resolve_falls_back_to_defaults_when_no_row():
    roles, threshold, window = resolve_prefs(None, _defaults())
    assert roles == ["Software Engineer"]
    assert threshold == 0.6
    assert window == 2


def test_resolve_empty_db_roles_fall_back_to_defaults():
    prefs = SimpleNamespace(
        target_roles=[], job_fit_threshold=0.7, review_window_hours=1.0
    )
    roles, threshold, window = resolve_prefs(prefs, _defaults())
    # Empty target_roles in DB should not blank out the prefilter.
    assert roles == ["Software Engineer"]
    assert threshold == 0.7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline/test_prefs_resolver.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_prefs'`

- [ ] **Step 3: Change the scorer signature**

In `app/scrapers/jobs/scorer.py`:
- Rename the module constant `CANDIDATE_PROFILE` to `_DEFAULT_PROFILE` (keep the same string content).
- Change `score_job` to accept an optional profile and fall back to the default:

```python
def score_job(title: str, jd_text: str, profile: str | None = None) -> tuple[float, str]:
    """Score a job posting against a candidate profile.

    Returns (score: float 0-1, reasoning: str).
    """
    prompt = SCORE_PROMPT.format(
        profile=profile or _DEFAULT_PROFILE,
        title=title,
        jd_text=jd_text[:3000],
    )
```

(The rest of `score_job` is unchanged.)

- [ ] **Step 4: Add `resolve_prefs` + wire profile into `run_job_scrape_pipeline`**

In `app/pipeline/jobs.py`:

Add imports near the top (after the existing model imports):

```python
from app.models.preferences import Preferences
from app.resumes.profile import build_candidate_profile
```

Add the pure resolver above `run_job_scrape_pipeline`:

```python
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
```

Then inside `run_job_scrape_pipeline`, replace the block currently at lines 60-62:

```python
    threshold = settings.job_fit_threshold
    role_terms = [r.lower() for r in settings.target_roles]
    deadline = datetime.now(timezone.utc) + timedelta(hours=settings.review_window_hours)
```

with:

```python
    prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
    role_names, threshold, review_window_hours = resolve_prefs(prefs, settings)
    role_terms = [r.lower() for r in role_names]
    deadline = datetime.now(timezone.utc) + timedelta(hours=review_window_hours)

    # Build the candidate profile once per run (resume text + target roles).
    profile = await build_candidate_profile(db)
```

Then change the scoring call (currently `:99`) from:

```python
            score, reasoning = score_job(raw["title"], raw.get("jd_text", ""))
```

to:

```python
            score, reasoning = score_job(raw["title"], raw.get("jd_text", ""), profile)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_pipeline/test_prefs_resolver.py tests/test_job_pipeline.py tests/test_job_scrapers.py -v`
Expected: PASS. If `test_job_pipeline.py` patches `score_job` or seeds companies, confirm it still passes with the new signature (the third arg is optional, so existing 2-arg patches/calls remain valid).

- [ ] **Step 6: Commit**

```bash
git add app/scrapers/jobs/scorer.py app/pipeline/jobs.py tests/test_pipeline/test_prefs_resolver.py
git commit -m "feat: job pipeline scores against resume+prefs profile, reads DB preferences"
```

---

## Task 7: Dry-run apply path

**Files:**
- Modify: `apply_worker/main.py:130-148`

- [ ] **Step 1: Guard the submit + status in dry-run**

In `apply_worker/main.py`, replace the "Submit form" block through the end of `process_job` (currently lines 130-148) with:

```python
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
            status=ApplicationStatus.SUBMITTED.value,
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
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `python -c "import apply_worker.main; print('ok')"`
Expected: `ok`

(No unit test here — `process_job` drives a live Playwright `page`; it is exercised in the Task 8 verification. Confirm `Application` has a `notes` column: `python -c "from app.models.application import Application; print(hasattr(Application, 'notes'))"` → `True`.)

- [ ] **Step 3: Commit**

```bash
git add apply_worker/main.py
git commit -m "feat: dry-run apply mode (fill+screenshot, no submit)"
```

---

## Task 8: Full-suite + end-to-end dry-run verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full real test suite**

Run: `python -m pytest tests/ -v`
Expected: all pass (was 41 pass / 1 fail; the 1 failure `test_settings_routes_registered` is now green, plus the new tests). The orphan `find-companies-using-ashby-job-boards/` package is excluded by scoping to `tests/`.

- [ ] **Step 2: Boot the app**

Run: `python -c "from app.main import app; print(len(app.openapi()['paths']), 'paths')"`
Expected: a path count well above the previous 5 mounted routes (all 6 routers now contribute paths).

- [ ] **Step 3: End-to-end dry-run (requires DB + Redis + Playwright)**

This exercises the real loop safely. If infra is unavailable in this environment, document the exact steps and defer to the user.

```bash
# 1. Migrate
alembic upgrade head

# 2. Seed a company with a real ATS slug (example: a known greenhouse board)
#    via `python seed.py` or a direct insert, ensuring ats_type + ats_slug set.

# 3. Upload a resume (marks is_default) and confirm extracted_text is populated:
#    curl -F "file=@resume.pdf" -F "is_default=true" http://localhost:8000/api/resumes

# 4. Enable dry-run, boot the API, and trigger the job fetch:
export APPLY_DRY_RUN=true
uvicorn app.main:app  &   # in one shell
curl -X POST http://localhost:8000/api/pipeline/run   # trigger fetch→score→IN_REVIEW

# 5. Approve a job via API (moves it to APPROVED):
curl -X POST http://localhost:8000/api/jobs/<job_id>/approve
#    review_deadline_check (every 15m) enqueues APPROVED jobs to Redis apply_queue.
#    Then run the apply worker in dry-run:
APPLY_DRY_RUN=true python -m apply_worker.main
```

- [ ] **Step 4: Assert dry-run outcome**

Confirm: an `Application` row exists with `notes = "dry_run"`, a screenshot file was written under `apply_worker_screenshot_dir`, the associated `Job.status` is still `APPROVED` (not `APPLIED`), and no application was actually submitted.

- [ ] **Step 5: Final commit (if any verification fixups were needed)**

```bash
git add -A
git commit -m "chore: Phase 1 end-to-end dry-run verification"
```

---

## Notes / Constraints

- `score_job` is synchronous (`llm_complete` is sync); the added `profile` arg is a plain string — no async change.
- The DB-dependent steps (migration, e2e) require `database_url`/`redis_url`/Playwright to be reachable. Where they are not, the code + migration are still committed and verification is deferred with a clear note — never claim a step passed without running it.
- Preferences `review_window_hours` is a `Float`; `timedelta(hours=float)` is valid.
- Out of scope (Phase 2): LangGraph subgraph refactor, NGO-jobs subgraph, outreach delivery, gmail write scope.
