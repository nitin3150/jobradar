# JobRadar

Self-hosted, single-user AI job-hunting platform. Discovers startup job boards, scores postings against your profile via LLM, semi-automates applications through a hybrid review queue, submits forms with Playwright, tracks recruiter replies through Gmail.

## Stack

| Layer | Tech |
|---|---|
| API + scheduler | FastAPI, APScheduler, SQLAlchemy 2 async, Alembic |
| Storage | Postgres 16, Redis 7 |
| LLM gateway | LiteLLM — NVIDIA NIM (primary) → Groq (fallback) |
| Form submission | Separate `apply-worker` process running Playwright |
| Email tracking | Gmail API (read-only) + LiteLLM reply classifier |
| Frontend | Vite + React 19 + TanStack Query + Tailwind 4 |

## Architecture

Five Docker services: `postgres`, `redis`, `backend`, `apply-worker`, `frontend`.

The **backend** runs three long-lived pipelines on a schedule:

- **Funding graph** — LangGraph pipeline at 8 AM ET daily. Discovers recently-funded startups (YC, TechCrunch, Crunchbase, SEC EDGAR, Twitter, ProductHunt, HackerNews), enriches with website description + Twitter signals, LLM-scores hiring intent, upserts `Company` rows.
- **Job scraper** — every hour, configurable via `PUT /api/pipeline/schedule` (Redis-overridable). For every Company with an ATS board, pulls Ashby / Lever / Greenhouse / (extensible) postings, LLM-scores fit against your profile, inserts `Job(in_review, review_deadline)`.
- **ATS discovery** — every 24 h. Generates `site:` Google queries via Serper (preferred when `SERPER_API_KEY` is set) or Playwright fallback, extracts slugs, attaches new `(board, slug)` pairs to existing companies.

Plus two 15-minute scheduled tasks:

- **`review_deadline_check`** — expires in-review Jobs past their deadline. Default action: **reject** (configurable to auto-approve via `Settings.review_deadline_action`).
- **`gmail_poll`** — fetches new replies tagged with the Gmail label `job-applications`, LLM-classifies them as interview / rejection / other, patches `Application.status`.

See [`diagrams/pipeline-architecture.mmd`](diagrams/pipeline-architecture.mmd) for the full system diagram (auto-rendered to `.svg` / `.png`), or [`diagrams/job-lifecycle.mmd`](diagrams/job-lifecycle.mmd) for a sequence diagram of one job's complete lifecycle.

## One job's lifecycle

1. **Discovery** attaches a `(board, slug)` to an existing company (24 h).
2. **Hourly scraper** pulls the board, LLM-ranks, inserts `Job(in_review)` with a 2-hour review window (default).
3. **You** open `/jobs`, click Approve → Redis `apply_queue`.
4. **apply-worker** `BRPOP`s the job, Playwright opens the URL, fills the form using Q&A bank matches (rapidfuzz keyword → LiteLLM semantic fallback), Submits, screenshots the confirmation page, inserts `Application(submitted)`.
5. Days later, a recruiter replies → the **Gmail poller** picks up the thread, LLM-classifies, `Application.status` updates to interview / rejected / no-op.

## Quickstart

```bash
# Prereqs: Docker + Docker Compose, plus an API key for at least one of NVIDIA / Groq
docker compose up --build
```

The first run will fail unless you have a `.env` at the repo root (or `backend/.env`). At minimum it must contain:

```env
DATABASE_URL=postgresql+asyncpg://fundingradar:secret@localhost:5432/fundingradar
REDIS_URL=redis://localhost:6379/0
NVIDIA_API_KEY=<your-key>      # OR GROQ_API_KEY — at least one provider must work
```

The full list of optional keys (Apify for enrichment, Serper for discovery, Gmail polls, ATS feature toggles, etc.) lives in `backend/app/config.py` — `Settings` is the single source of truth. Every field is documented as a comment above the declaration.

Open once it's up:

- Frontend: <http://localhost:3000>
- API + Swagger: <http://localhost:8000/docs>- Postgres: `localhost:5432` (`fundingradar:secret`, database `fundingradar`)
- Redis: `localhost:6379```

To run pieces locally without Docker:
```bash
# Backend (needs reachable Postgres + Redis)
cd backend && uvicorn app.main:app --reload
# Worker in another terminal
cd backend && python apply_worker/main.py
# Frontend
cd frontend && npm run dev
```

## Where to look

| Want to understand … | File |
|---|---|
| Fit scoring (LLM ranker) | `backend/app/scrapers/jobs/scorer.py` |
| Form auto-fill + Q&A matching | `backend/apply_worker/qa_matcher.py`, `apply_worker/form_filler.py` |
| Scheduler orchestration | `backend/app/scheduler.py` |
| LLM gateway + primary → fallback | `backend/app/llm/client.py` |
| The three pipelines | `backend/app/pipeline/{graph.py, jobs.py, discovery.py}` |
| Frontend ↔ backend wiring | `frontend/src/api/`, `frontend/src/hooks/` |
| DB schema | `backend/app/models/` + Alembic migrations under `backend/alembic/versions/` |

## Development

- Backend tests: `cd backend && pytest tests/ -q`
- Frontend tests: `cd frontend && npm test`
- Lint: `cd frontend && npm run lint`
- New migrations: `cd backend && alembic revision --autogenerate -m "<msg>"` — then review for column types and indexes
- Q&A bank seed (run once): `cd backend && python seed_qa_bank.py` after filling in your answers
- Diagram source of truth: edit `diagrams/*.mmd` and re-render with `npx -y @mermaid-js/mermaid-cli`

## License

Personal project — not currently licensed for redistribution.
