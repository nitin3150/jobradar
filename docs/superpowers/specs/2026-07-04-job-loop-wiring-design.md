# Phase 1 — Wire the Job→Apply Loop End-to-End

**Date:** 2026-07-04
**Status:** Approved (design)
**Scope:** Phase 1 of a two-phase effort. Phase 1 makes the job-intelligence loop work end-to-end by wiring together career-ops pieces that already exist in the repo but are disconnected. Phase 2 (separate spec) refactors the job flow into a LangGraph subgraph architecture.

## Context

The jobradar backend contains two parallel systems:

- **Company/funding pipeline** — a real LangGraph (`app/pipeline/graph.py`): `funding_detector → enrichment → scorer → save`. Company-oriented. Run daily by APScheduler.
- **Job-intelligence flow** — ATS fetch, ATS-slug discovery, auto-apply worker, Gmail polling. Built as plain async functions driven by APScheduler + a Redis `apply_queue`, entirely **outside** LangGraph.

Empirical validation (2026-07-04) confirmed the "built but not wired" hypothesis:

- Test suite: **41 pass, 1 fail**. The single failure is `test_settings_routes_registered`, which fails *because* the settings router is commented out — the gap already has a test.
- App boots (`from app.main import app` → 5 routes). Router imports are all live; only the `include_router` mount lines are commented (uncommitted working-tree edits from today) — deliberate "not yet enabled," not "broke the build."
- No PDF/docx extraction dependency present → resume text extraction is the one net-new build.

**Correction to prior notes:** JobSpy (`python-jobspy`) is **not** integrated in this repo (zero imports). Actual discovery = `app/scrapers/jobs/search.py` doing serper.dev / headless-Playwright *ATS-slug* discovery, then direct ATS JSON APIs fetch jobs.

## Decisions (from brainstorming)

- **"Career-ops features"** = the pieces already in this repo (apply_worker, resume_selector, preferences, qa_bank, gmail classifier). Not an external design.
- **Goal:** "Both, in order" — Phase 1 makes it work end-to-end (this spec); Phase 2 does the LangGraph refactor.
- **Apply gate:** Keep auto-approve-on-timeout (current behavior). After `review_window_hours`, `review_deadline_action` decides; approved jobs auto-enqueue and submit.
- **Scoring input:** Candidate profile derived from the **default Resume text + Preferences target_roles** (most "career-ops"; requires net-new resume text extraction).
- **Approach A** — minimal wire-through. Profile builder written as one standalone function so Phase 2 reuses it without a rewrite.

## Design

### S1 · Mount routers + config hygiene

- Uncomment the 6 `include_router` lines in `app/api/router.py:14-20`: jobs, applications, qa-bank, resumes, settings, outreach.
- Re-enable `/screenshots` StaticFiles mount in `app/main.py`.
- Add missing fields to `Settings` in `app/config.py` (all `str | None = None`): `serper_api_key`, `apify_api_key`, `gmail_token_path`, `gmail_credentials_path`, `gmail_label`. These are referenced in code (`discovery.py`, `enrichment.py`, `twitter_scraper.py`, `gmail/connector.py`) but undefined → `AttributeError` when those paths run. `extra="ignore"` ignores unknown env vars but does not create attributes.
  - **Blast radius:** only `serper_api_key` gates the core loop (ATS-slug discovery). `apify_api_key` (company enrichment/Twitter) and gmail settings are off the fetch→score→review→apply critical path. All fixed here regardless (cheap), but core loop only needs serper.
- **Verify:** `test_settings_routes_registered` flips green; add same-style route-registration tests for the other 5 mounts.

### S2 · Resume text extraction (net-new)

- Add deps `pypdf`, `python-docx` to `pyproject.toml`.
- New module `app/resumes/extract.py`: `extract_text(path: Path, content_type: str) -> str`.
  - pdf → pypdf; docx → python-docx; txt/md → read directly.
  - Bounded output (cap ~20k chars). Any failure returns `""` — extraction never blocks or fails an upload.
- Alembic migration `007`: add `Resume.extracted_text` (Text, nullable).
- Extract **at upload time** in `app/api/resumes.py::upload_resume`; cache result to `extracted_text`. Never extract per-score (would re-parse PDFs every fetch cycle).

### S3 · Candidate profile builder + scorer wiring

- New module `app/resumes/profile.py`: `build_candidate_profile(db) -> str`.
  - Pulls the default `Resume.extracted_text` + Preferences `target_roles`, composes the profile string.
  - Fallback to the current hardcoded constant (renamed `_DEFAULT_PROFILE`) when there is no default resume and no preferences — scoring never breaks.
- Change scorer signature `score_job(title, jd_text)` → `score_job(title, jd_text, profile)` in `app/scrapers/jobs/scorer.py`. Remove module-level `CANDIDATE_PROFILE` (retain as `_DEFAULT_PROFILE` fallback used by the builder).
- `app/pipeline/jobs.py` builds the profile **once per run** and passes it into the scoring loop.

### S4 · Preferences → job pipeline from DB

- `app/pipeline/jobs.py:60-62` currently reads `target_roles`, `job_fit_threshold`, `review_window_hours` from env `settings`. Change to read from the DB Preferences singleton row, falling back to env `settings` when the row is absent.
- One helper `load_effective_prefs(db)` returns the resolved values. Keeps the pipeline consistent with `resume_selector`, which already reads DB Preferences.

### S5 · Dry-run apply (verification safety)

- Add `apply_dry_run: bool = False` to `Settings` + `APPLY_DRY_RUN` env var.
- In `apply_worker/main.py`: when dry-run is on, perform every step (navigate, fill fields, attach resume, screenshot, create `Application` row with a `dry_run` note) **except** the final submit click, and do not set `Job.status = APPLIED` (use a distinct marker/note).
- Rationale: production keeps auto-approve + real submit (user's choice), so the loop cannot be verified end-to-end with real submissions during development. Dry-run provides a safe verification path.

### S6 · Test + verify

- **Unit:** `extract_text` (pdf/docx/txt fixtures), `build_candidate_profile` (with/without default resume, with/without prefs, fallback), `load_effective_prefs` (DB row vs env fallback), `score_job` new signature.
- **Integration:** route-registration tests for all 6 newly mounted routers; job pipeline uses DB prefs.
- **End-to-end (dry-run):** seed a company with an ATS slug → run job fetch → score → `IN_REVIEW` → approve → apply worker in dry-run → assert `Application` row + screenshot exist and no submit occurred.

## Out of scope (Phase 2)

- LangGraph subgraph refactor of the job flow (ATS ingestion / discovery / NGO / scoring / action / outreach subgraphs under one graph).
- NGO-jobs subgraph (NGO scrapers currently feed the *company* pipeline as company dicts, not jobs).
- Outreach delivery/sending (no email/DM sender exists; `send_followup_emails` preference has no consumer).
- Gmail write scope (currently `gmail.readonly`, poll-only).
- The orphan `find-companies-using-ashby-job-boards/` standalone package (not imported by the app).

## Risks / notes

- `score_job` is synchronous (`llm_complete` is sync) and called inside the async loop — signature change is mechanical; no async conversion needed.
- Migration `007` follows linear head `006`.
- Mounting routers exposes previously-dead HTTP surface; route-registration tests guard against import/wiring regressions.
