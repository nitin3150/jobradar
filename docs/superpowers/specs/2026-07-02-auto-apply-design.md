# Auto-Apply Extension — Design Spec

**Date:** 2026-07-02  
**Project:** jobradar  
**Scope:** Extend jobradar with automated job application submission, application tracking, Q&A bank, and Gmail reply tracking.

---

## Goal

Add aiapply-style automation to jobradar:
1. Scrape job listings from Ashby/Lever/Greenhouse for companies already in jobradar DB
2. Queue matched jobs for review, auto-submit after review window expires
3. Track application statuses, updated automatically via Gmail

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  jobradar FastAPI (existing + extended)          │
│                                                  │
│  ┌──────────────┐  ┌───────────────────────────┐ │
│  │ job scrapers │  │ scheduler (APScheduler)   │ │
│  │ ashby/lever/ │  │ - scrape jobs (hourly)    │ │
│  │ greenhouse   │  │ - gmail poll (15 min)     │ │
│  └──────┬───────┘  └───────────────────────────┘ │
│         │ enqueue                                 │
│  ┌──────▼──────────────────────────────────────┐ │
│  │              Redis                           │ │
│  │       apply_queue  |  results                │ │
│  └──────────────────────────┬──────────────────┘ │
└─────────────────────────────│────────────────────┘
                              │ dequeue
┌─────────────────────────────▼────────────────────┐
│  apply-worker (separate process, same repo)       │
│                                                   │
│  Playwright browser                               │
│  Q&A bank matcher                                 │
│  form filler → submit / flag                      │
│  writes Application record to shared Postgres DB  │
└───────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  Gmail connector (scheduler task in FastAPI)     │
│  Gmail API (readonly) → match replies            │
│  → update Application.status                    │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  React frontend (existing + 3 new panels)        │
│  - Jobs review queue (approve / reject)          │
│  - Application tracker                           │
│  - Q&A bank manager                              │
└─────────────────────────────────────────────────┘
```

**Postgres DB** shared between FastAPI and apply-worker. Worker writes results directly; FastAPI reads and serves them.

---

## Data Models

### Job
```python
Job:
  id: int
  company_id: int (FK → Company)
  title: str
  url: str
  ats_type: enum(ashby, lever, greenhouse)
  jd_text: str
  ai_fit_score: float
  status: enum(pending, in_review, approved, rejected, applied, flagged)
  scraped_at: datetime
  review_deadline: datetime  # scraped_at + configurable hours
```

### Application
```python
Application:
  id: int
  job_id: int (FK → Job)
  submitted_at: datetime
  submission_screenshot_path: str | null
  status: enum(submitted, interview, rejected, offer, ghosted)
  gmail_thread_id: str | null
  last_email_at: datetime | null
  notes: str | null
```

### QABankEntry
```python
QABankEntry:
  id: int
  question_pattern: str      # fuzzy match key (e.g. "authorized to work")
  canonical_question: str    # normalized display form
  answer: str | null         # null = unknown, flagged for user to fill
  answer_type: enum(text, boolean, number, select)
  times_used: int
  last_used_at: datetime | null
```

---

## Apply Worker

**Process:** `apply_worker/main.py` — separate entrypoint, same repo, shares Postgres + Redis.

**Loop:**
```
job = redis.brpop("apply_queue", timeout=0)
open job.url in Playwright
fields = extract_form_fields()

for each field:
  match = fuzzy_match(field.label, QABankEntry)
  if match.score > threshold:
    fill(field, match.answer)
  else:
    save QABankEntry(question=field.label, answer=null)
    flag job → manual_review
    abort submission

if no flags:
  submit()
  screenshot()
  write Application(status=submitted)
else:
  write Job(status=flagged)
```

**Q&A Matching — two-pass:**
1. Exact/keyword match on `question_pattern` (no API call)
2. Claude similarity check if pass 1 fails — score threshold: 0.75 (configurable via `QA_MATCH_THRESHOLD` env var)

Unknown questions saved to bank with `answer=null`. User fills them in Q&A manager UI once — auto-answered in all future applications.

---

## Gmail Connector

**Trigger:** APScheduler task every 15 minutes.

**Flow:**
```
fetch Gmail threads matching label "job-applications" or subject pattern
for each new thread:
  find Application by gmail_thread_id or subject/sender match
  classify reply via Claude (interview / rejection / other) — 1 call per new thread
  update Application.status
  set Application.gmail_thread_id, last_email_at
```

**OAuth scope:** `gmail.readonly` only — no write access required.

**Setup prerequisite:** Create a Gmail filter rule that labels all sent job application emails with `job-applications`. The connector matches replies to this label. One-time manual setup.

---

## Frontend Panels

### Jobs Review Queue (`pages/JobsReview.jsx`)
- Card per job: title, company, fit score, ATS type, time remaining in review window
- Actions: Approve (→ `apply_queue`) | Reject (dismiss)
- Badge on nav showing pending count
- Auto-refresh every 60s

### Application Tracker (`pages/ApplicationTracker.jsx`)
- Table: job title, company, submitted date, status
- Status auto-updated by Gmail connector
- Manual status override via dropdown
- Filter by status, date range
- Click row → shows submission screenshot

### Q&A Bank Manager (`pages/QABank.jsx`)
- Table of all `QABankEntry` rows
- Unanswered entries (flagged unknowns) pinned to top, highlighted
- Inline edit answer → save → optionally re-queue flagged job
- Add new entries manually
- Shows `times_used` per entry

**Navbar:** extend existing with badge counter on "Review" link.

---

## Job Scrapers

3 new scrapers added to `backend/app/scrapers/jobs/`:
- `ashby.py` — scrapes `jobs.ashbyhq.com/{slug}`
- `lever.py` — scrapes `jobs.lever.co/{slug}`
- `greenhouse.py` — scrapes `boards.greenhouse.io/{slug}`

**Prerequisite:** `Company` model needs two new fields: `ats_type: enum(ashby, lever, greenhouse) | null` and `ats_slug: str | null`. These are set manually (or via a future discovery step) per company. Scrapers skip companies where `ats_type` is null.

Each scraper:
1. Iterates companies in DB with matching ATS type and non-null slug
2. Fetches job listings, deduplicates by URL
3. Passes JD text to Claude for fit scoring
4. Creates `Job` records with `status=in_review` and `review_deadline`

Triggered hourly via APScheduler.

---

## Review Window

- Configurable duration (default: 2 hours)
- Jobs in `in_review` state appear in dashboard
- On deadline: auto-approve (submit) or auto-reject — configurable per user preference
- Default: auto-reject on deadline (safer for personal use)

---

## File Structure Changes

```
jobradar/
  backend/
    app/
      scrapers/
        jobs/
          ashby.py        # new
          lever.py        # new
          greenhouse.py   # new
      models/
        job.py            # new
        application.py    # new
        qa_bank.py        # new
      api/
        jobs.py           # new
        applications.py   # new
        qa_bank.py        # new
      gmail/
        connector.py      # new
        classifier.py     # new
    apply_worker/
      main.py             # new — worker entrypoint
      form_filler.py      # new — Playwright form interaction
      qa_matcher.py       # new — two-pass Q&A matching
  frontend/
    src/
      pages/
        JobsReview.jsx        # new
        ApplicationTracker.jsx # new
        QABank.jsx            # new
```

---

## Out of Scope (Future)

- Resume tailoring per application (B from original options)
- LinkedIn / Indeed auto-apply (non-ATS platforms)
- Multi-user / SaaS features
