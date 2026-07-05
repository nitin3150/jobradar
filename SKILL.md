# JobRadar — Agent Skill

## What this project does
Self-hosted, single-user AI job-hunting platform. Discovers startup job boards, scores postings against your profile via LLM, submits applications through Playwright with a hybrid review queue, tracks recruiter replies through Gmail.

## Stack
- **Backend**: FastAPI + APScheduler + SQLAlchemy 2 async + Alembic, Python 3.12+
- **Storage**: Postgres 16, Redis 7
- **Worker**: separate `apply-worker` process running Playwright; shares Postgres + Redis
- **LLM gateway**: LiteLLM — primary = OpenAI-compat endpoint on `https://integrate.api.nvidia.com/v1` (model `openai/z-ai/glm-5.2`), fallback = Groq `llama-3.3-70b-versatile`
- **Frontend**: Vite + React 19 + TanStack Query + Tailwind 4
- **Email**: Gmail API readonly, reply classifier reuses the same LLM gateway

## Directory map (where to put things)
- HTTP routes → `backend/app/api/<resource>.py`, register in `api/router.py`
- Long-running pipelines → `backend/app/pipeline/{graph.py, jobs.py, discovery.py}`
- Scraper → `backend/app/scrapers/<name>.py` + add class to `scrapers/__init__.py:ALL_SCRAPERS` + set `enabled_setting` class attr + add `Settings.scraper_<name>_enabled` field
- SQLAlchemy ORM → `backend/app/models/<resource>.py` (DB tables only, no Pydantic)
- Pydantic schemas → `backend/app/schemas/<resource>.py` (request/response only)
- LLM calls → always `from app.llm.client import llm_complete` — never import `litellm` directly
- Frontend fetches → `frontend/src/hooks/use<X>.js` + `frontend/src/api/<resource>.js`
- Frontend component → `frontend/src/components/<Name>.jsx`; modal under `components/modals/`

## Data flow (causal chain, do not break)
1. `pipeline/discovery.py` (every 24 h, Serper / Playwright / Apify) → finds new `(ats_type, ats_slug)` pairs → attaches to existing Companies
2. `pipeline/jobs.py` (every hour, parallel semaphore = 10) → pulls every board concurrently (network-bound) → 2nd phase is sequential (single AsyncSession, blocking LLM call) → title prefilter → fit-score → upsert `Job(in_review, review_deadline)`
3. `frontend/pages/JobsReview.jsx` → `POST /api/jobs/{id}/approve` → `Redis RPUSH apply_queue`
4. `apply_worker/main.py` → `BRPOP apply_queue` → Playwright opens job URL → for each form field: rapidfuzz keyword match → fallback to LiteLLM semantic match
   - if all match → click Submit, take full-page screenshot, insert `Application(submitted)`, set `Job.status = applied`
   - if any unknown → insert `QABankEntry(answer=None)`, set `Job.status = flagged` (do not submit)
5. `scheduler.run_review_deadline_check` (every 15 min) → expires `Job.in_review` past `review_deadline` → auto-reject (or auto-approve if `review_deadline_action = "approve"`)
6. `scheduler.run_gmail_poll` (every 15 min) → fetches new replies matching label `job-applications` → LiteLLM classifies → `Application.status` updates to interview / rejected / no-op

## Common tasks

### Add a startup discovery scraper
1. Subclass `BaseScraper` in `backend/app/scrapers/<name>.py`. Set `enabled_setting = "scraper_<name>_enabled"` as a class attribute — scheduler reads this dynamically.
2. Add the class to `ALL_SCRAPERS` in `scrapers/__init__.py`.
3. Add `scraper_<name>_enabled: bool = True` to `Settings` in `config.py`.

### Add a new ATS fetcher (e.g. Workday)
1. New fetcher in `backend/app/scrapers/jobs/workday.py` returning `list[dict]` shaped `{title, url, jd_text, ats_type}` — match existing ashby/lever/greenhouse exactly. Naming convention: `fetch_<ats_type>_jobs`.
2. Extend the `_ATS_TYPES` tuple in `backend/app/pipeline/jobs.py` (the static allowlist) so `_get_fetcher()` accepts the new type. `_get_fetcher()` does a `globals().get(f"fetch_{ats_type}_jobs")` dispatch — keep the allowlist and the named fetcher in sync.
3. Add `"workday"` to `settings.discovery_boards` and to `BOARD_SEARCH_DOMAINS` in `pipeline/discovery.py`.

### Change the LLM primary / fallback
Add a new entry to `PROVIDERS` in `app/llm/client.py` and corresponding `*_api_key` / `*_model` / `*_base_url` fields in `config.py`. Selectors: `settings.llm_provider` and `settings.llm_fallback_provider`. Fallback triggers on any exception during the primary call.

### Tweak the review window
- Global: `review_window_hours` (default `2`) in `config.py`
- Per-user: `Preferences.review_window_hours` via `PATCH /api/preferences`
- Deadline action: `review_deadline_action` ∈ {`"approve"`, `"reject"`} — default `"reject"`

### Debug: a scraper suddenly returns nothing
1. `docker compose logs backend` — every scraper logs `f"{name} failed: {e}"` on exceptions
2. Ashby / Lever / Greenhouse / YC / HackerNews / TechCrunch RSS render server-side → httpx + bs4 only, **do not** escalate to Playwright unless you have first-hand evidence the page is JS-rendered (see SKILL.md top comment in old `job_hunter.py` — that wisdom still holds)
3. Algolia search endpoints sometimes drop filter params from `numericAttributesForFiltering`; if `numericFilters="x>100"` returns 400, filter the count client-side (see `hackernews.py`)

### Debug: apply-worker won't submit
1. `docker compose logs apply-worker` — the BRPOP loop logs at ERROR on exception, INFO on success
2. Job → `flagged`: open `/qa-bank`, fill the orange (unanswered) entry, re-approve from `/jobs?status=flagged`
3. Job stuck in `approved`: probably hit a CAPTCHA. Either accept manually in the browser, or seed a `QABankEntry` answer that explains CAPTCHA handling and re-queue

### Debug: a page does not render / infinite loading in the browser
- All frontend data goes through TanStack Query hooks. Default `refetchInterval` is 60 s for jobs/applications, 30 s for the navbar pending-count
- CORS origins: `settings.cors_origins` (defaults `localhost:3000` + `localhost:5173`). If you proxy differently, add your origin there
- API base is set at frontend-build time via `VITE_API_URL` (defaults `http://localhost:8000`)

## Conventions
- **Pydantic schemas go in `app/schemas/`, never `app/models/`.** The original codebase mistakenly put four Pydantic classes in `app/models/pipeline.py` — they were migrated to `app/schemas/pipeline.py`. Do not re-introduce
- **All LLM calls go through **`app.llm.client.llm_complete`**.** That module is the only file in the project that imports **`litellm`** directly. New code must only call **`llm_complete`**; do not add another direct **`litellm`** importer anywhere else. The wrapper handles the primary→fallback chain and the per-provider key routing
- **One FastAPI router per resource, mounted under `/api`.** Sub-routers live next to each other in `app/api/`. DB sessions flow through `Depends(get_db)`. Long-lived singletons (the shared `httpx.AsyncClient`, Redis pool, Playwright browser, APScheduler instance) live on `app.state.X` and are read inside handlers via `request: Request` — these are not the same as DB access
- **`Settings` (pydantic-settings) is the single source of env keys.** Empty-string defaults for optional secrets so code degrades gracefully. Field name `my_key` ↔ env `MY_KEY`
- **Time storage is always UTC.** `DateTime(timezone=True)` columns; no naive datetimes anywhere
- **Migrations are forward-only.** Alembic revisions in `backend/alembic/versions/`, naming `NNN_<verb>_<noun>.py`. Never edit an applied migration — write a new one
- **Frontend data fetching goes through TanStack Query hooks.** No direct axios calls in JSX. If a hook doesn't exist for a resource, write one in `frontend/src/hooks/`

## Testing
- Backend: `cd backend && pytest tests/ -q` — covers scheduler, scraper mocks, qa_matcher, preferences, search_backend
- New revisions: `cd backend && alembic revision --autogenerate -m "<msg>"` then manually edit for column types + indexes
- Frontend: `cd frontend && npm test` (Vitest + Testing Library, two existing `*.test.jsx` files)

## Diagram source of truth
- `diagrams/pipeline-architecture.mmd` — system-level architecture (regenerate `.svg` / `.png` with `npx -y @mermaid-js/mermaid-cli`)
- `diagrams/job-lifecycle.mmd` — sequence diagram of one job's full lifecycle
- Both diagrams can also be regenerated by pasting the `.mmd` into https://mermaid.live
