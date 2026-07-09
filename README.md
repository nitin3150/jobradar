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

## Supabase setup (production target)

JobRadar's persistent state lives on **Supabase**: the Postgres database, the `resumes` Storage bucket, and the project-level secrets. Start there before running anything local.

1. **Create a project.** Sign up at [database.new](https://database.new) and spin up a new project. Note the project reference (looks like `abcdefghijkl`).
2. **Get the connection string.** Open **Settings → Database → Connection string → URI** in the Supabase dashboard. Pick **“Transaction pooler”** (port `6543`) — it handles the most concurrent connections and the FastAPI/asyncpg stack is built for it. Replace `postgresql://` with `postgresql+asyncpg://` when you paste it into `.env`; `backend/db/migrations/env.py` will additionally append `prepared_statement_cache_size=0` when it sees the pooler URL, which is what asyncpg needs to play nicely with pgBouncer.
3. **Get the storage + auth keys.** In **Settings → API**, copy the **Project URL** and the **`service_role` secret** into `.env` as `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. The `service_role` key is a server-side secret — never expose it to the React frontend. (`SUPABASE_ANON_KEY` is optional; skip unless you ever expose public reads.)
4. **Apply the schema.** Either path works and produces the same surface — keep them in sync when you make future schema changes:

   ```bash
   # Path A — Alembic (matches the backend's declarative models 1-for-1)
   cd backend && alembic upgrade head

   # Path B — Supabase CLI (flat SQL mirrors the Alembic migration)
   supabase db push
   ```

   The schema lives at `backend/db/models.py` + `backend/db/migrations/versions/0001_initial_schema.py` (Alembic) and `supabase/migrations/20260101000000_initial_schema.sql` (Supabase CLI mirror). The Storage bucket for resumes is auto-created by `supabase/migrations/20260101000000_storage_resumes_bucket.sql`.

5. **Optional but recommended** — install the [Supabase CLI](https://github.com/supabase/cli) (`brew install supabase/tap/supabase` or `npm i supabase -g`) so the `supabase db push` / `supabase db reset` workflow matches the rest of the stack.

`docs/project-overview.md` §7.3 has the full picture: tables, enum types, indexing strategy, RLS decision rationale (off, because single-user), and how to add a new migration in lockstep across both representations.

## Quickstart (local dev)

```bash
# Prereqs: Docker + Docker Compose, plus a Supabase project (see above) and
# an API key for at least one of NVIDIA / Groq.
docker compose up --build
```

The first run will fail unless you have a `.env` at the repo root (or `backend/.env`) with the Supabase keys set. At minimum:

```env
DATABASE_URL=postgresql+asyncpg://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<your-service-role-secret>
REDIS_URL=redis://localhost:6379/0
GROQ_API_KEY=<your-key>     # OR NVIDIA_API_KEY — at least one LLM provider must work
```

The full list of optional keys (Apify for enrichment, Serper for discovery, Gmail polls, ATS feature toggles, etc.) lives in `.env.example`. Every field there has an inline comment explaining what it does.

Open once it's up:

- Frontend: <http://localhost:3000>
- API + Swagger: <http://localhost:8000/docs>
- Redis: `localhost:6379` (local container)

The Postgres database is **not** a Docker service anymore — it's on Supabase. To inspect it live, use the Table Editor (browser) or the SQL Editor (browser); the `supabase` CLI gives you `supabase db pull` to round-trip schema into a local file.

To run pieces locally without Docker:
```bash
# Backend (points at Supabase via DATABASE_URL)
cd backend && uvicorn main:app --reload
# Worker in another terminal (uses SUPABASE_URL/SERVICE_ROLE_KEY for resume uploads)
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
