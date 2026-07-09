# JobRadar — Project Overview

A living reference for the entire JobRadar system: what it does, how it does it, what
endpoints exist, what runs at each layer, what external resources it consumes, what
techniques it uses to stay tractable, and how a single job moves from discovery to a
recruiter reply.

> **Audience.** New engineers / agents needing to understand the codebase before making
> changes. The root `README.md` is the high-level elevator pitch; this document is the
> full reference. The two backend/frontend micro-READMEs cover install/quickstart; here
> we wire everything together.

---

## 1. What JobRadar does

JobRadar is a **self-hosted, single-user AI job-hunting platform** for someone who wants
to find startup opportunities that aren't already on the big-aggregator websites:

1. **Discovers** recently-funded startups using a LangGraph pipeline (sources include
   YC, TechCrunch, Crunchbase, SEC EDGAR, Twitter, ProductHunt, HackerNews).
2. **Enriches** each lead with website description + Twitter signals, then LLM-scores
   hiring intent.
3. **Scrapes job boards** (Ashby / Lever / Greenhouse + extensible) for every saved
   lead, hourly, and LLM-ranks each posting against the user's preferences / profile.
4. **Surfaces** ranked postings in a review queue with a configurable deadline. Approve
   → the apply-worker (Playwright) opens the URL, fills forms using the Q&A bank,
   submits, screenshots the confirmation page.
5. **Tracks** the resulting applications in a tracker; every 15 minutes a Gmail poller
   fetches replies tagged `job-applications`, LLM-classifies them, and patches
   `Application.status` (interview / rejected / etc.).
6. **Stores** resumes and a Q&A bank server-side so multi-device sync is automatic.

The demo today is a **slice**: every backend router is wired and returns real data from
in-memory seeded stores, but the long-lived scheduler + Postgres + apply-worker + Gmail
poller are only fully exercised in the Dockerised pipeline. The React frontend consumes
exactly the wire shape those routers return, so when the persistence layer lands the
frontend doesn't change.

---

## 2. Stack overview

| Layer | Tech |
|---|---|
| Backend API | **FastAPI 0.139+**, **Pydantic 2.13+**, **uvicorn 0.30+** |
| Backend scraping | **httpx**, **BeautifulSoup4**, **LangGraph 1.2+**, **Playwright 1.61+** (apply-worker only) |
| Backend async / scheduling | (docs) APScheduler, dedicated worker process; not yet wired into the demo FastAPI process |
| Storage (Docker) | **Postgres 16**, **Redis 7** |
| In-demo storage | In-memory `dict` keyed by id + JSON-on-disk under `backend/data/` |
| Persistent storage (deployment) | **Supabase (Postgres 16)** via **SQLAlchemy 2 async + asyncpg**, schema managed by **Alembic** + mirrored to `supabase/migrations/` (see §7.3) |
| Object storage (deployment) | **Supabase Storage** bucket `resumes` accessed via the official `supabase` SDK wrapped in `backend/storage/supabase.py` |
| LLM gateway | **LiteLLM** with primary → fallback: **NVIDIA NIM** → **Groq** |
| Frontend runtime | **Vite 8**, **React 19**, **React Router 7**, **TanStack Query 5** |
| Frontend styling | **Tailwind CSS 4** |
| Frontend HTTP | **axios** |
| Frontend testing | **Vitest** + **@testing-library/react** + **jsdom** |
| Backend testing | `python -m unittest discover tests -v` (pytest not in `pyproject.toml`) |
| Logging | Custom FastAPI middleware + lifespan (see §4.2) |
| Containers | docker compose v2 (`docker-compose.yml`) — 5 services |

---

## 3. Repository layout

```
.
├── README.md                                  # High-level pitch
├── docker-compose.yml                         # 5-service Docker graph
├── .env  /  .env.example                      # Operator secrets (process env wins)
├── backend/
│   ├── main.py                                # FastAPI entry, middleware, lifespan
│   ├── pyproject.toml / requirements.txt      # Python deps
│   ├── Dockerfile                             # Python image for backend + apply-worker
│   ├── README.md                              # Backend install / API surface
│   ├── routes/                                # API layer; one module per router
│   │   ├── dashboard.py      # /api/dashboard/*
│   │   ├── scanner.py        # /api/scan/{funding|ngos|remote|oss|boards|/}
│   │   ├── outreach.py       # /api/outreach/{generate|{company_id}}
│   │   ├── companies.py      # /api/companies/{stats|...|{id}|{id}/status}
│   │   ├── pipeline.py       # /api/pipeline/{stats|status|schedule|discover|run}
│   │   ├── jobs.py           # /api/jobs/{pending-count|...|{id}/approve|{id}/reject}
│   │   ├── applications.py   # /api/applications/{...|{id}/status}
│   │   ├── qa_bank.py        # /api/qa-bank/{...|{id}}
│   │   ├── resumes.py        # /api/resumes/{...|{id}|{id}/download}
│   │   └── settings.py       # /api/settings/{PREFERENCES singleton}
│   ├── pipeline/                              # Domain scraping layer
│   │   ├── graph.py          # LangGraph assembly (4 parallel-feed nodes → merge)
│   │   ├── nodes/
│   │   │   ├── merge.py      # per-domain concordance → single merged list
│   │   │   ├── funding/      # Funding-news runner
│   │   │   ├── ngos/         # NGO boards runner
│   │   │   ├── remote/       # Remote-job runner
│   │   │   ├── oss/          # Open-source runner (GitHub Trending + GFI)
│   │   │   └── jobs_boards/  # Ashby/Lever/Greenhouse (separate runner, hourly)
│   │   └── …
│   ├── models/graph_state.py                 # TypedDict for the LangGraph state
│   ├── utils/                                 # http / time / filters / seen / logging
│   ├── cache/  /  db/                        # (planned) durable stores
│   ├── data/                                  # JSON-on-disk state (last_run, missing_orgs, …)
│   └── tests/                                 # unittest suite (177 tests in demo)
├── frontend/
│   ├── Dockerfile                             # node:20-alpine, Vite dev server
│   ├── package.json / package-lock.json
│   ├── vite.config.js                         # Vite + Tailwind; /api proxy → :8000
│   ├── index.html / src/
│   │   ├── main.jsx                           # createRoot + StrictMode
│   │   ├── App.jsx                            # Router + QueryClientProvider
│   │   ├── index.css                          # Tailwind 4 entry
│   │   ├── api/                               # axios wrappers per backend surface
│   │   │   ├── client.js                      # base axios instance, settings helpers
│   │   │   ├── jobs.js                        # /api/jobs*, /api/applications*, /api/qa-bank*
│   │   │   ├── resumes.js                     # /api/resumes* (incl. multipart upload)
│   │   │   └── scanner.js                     # /api/scan/* trigger helpers
│   │   ├── hooks/                             # React-Query wrappers (useXxx)
│   │   ├── pages/                             # Top-level routes (Dashboard, …)
│   │   ├── components/                        # Navbar, Card, Modal, StatusTracker, …
│   │   ├── contexts/                          # Cross-component state (Category)
│   │   └── test-setup.js                      # Vitest + @testing-library/jest-dom
│   └── README.md                              # (Vite template boilerplate)
└── docs/
    ├── project-overview.md                    # ← this file
    └── superpowers/                           # pre-existing
```

---

## 4. Architecture at a glance

Five Docker services glue together:

```
┌────────────────────────────────────────────────────────────────┐
│  browser  ───►  frontend (Vite + React)  :3000                 │
│                                                                 │
│  ┌──── proxy /api → backend :8000 ────┐                        │
│  │                                   │                         │
│  ▼                                   ▼                         │
│ backend (FastAPI)  ─── PG  ◄──────── postgres :5432            │
│   │  └─ APScheduler                  ▲                          │
│   │     ├─ funding graph (8 AM ET)   │                          │
│   │     ├─ jobs scraper (hourly)     │                          │
│   │     ├─ ats discovery (24 h)       │                          │
│   │     ├─ review_deadline_check (15m)│                         │
│   │     └─ gmail_poll (15 m)         │                          │
│   ▼                                   │                          │
│ redis :6379  (apply_queue, BRPOP) ────┘                          │
│   ▲                                                              │
│   │                                                              │
│ apply-worker (Python + Playwright) ──► writes Application rows  │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

Key boundaries:

- **FastAPI is the only HTTP surface.** Everything else is infrastructure (postgres,
  redis) or background work (apply-worker, gmail-poller) — both speak to the database
  directly, not over HTTP.
- **React Router + TanStack Query** own the frontend; no Redux/Zustand/Context for
  server state (only `CategoryContext` for cross-component UI state).
- **The in-memory CRUD stores in the demo are drop-in replacements** for what will
  eventually live in Postgres. The wire shape is stable; swap is mechanical.

---

## 5. Backend — FastAPI service

### 5.1 Entry point (`backend/main.py`)

In order:

1. **Environment loader** (`_load_env_files()`) — runs *before any other import* so
   `pipeline.nodes.oss.github_issues._cached_search` and other module-level env
   bindings see the values. Precedence (highest first): process env → `backend/.env`
   → `<repo-root>/.env`. `override=False` everywhere. Tested via
   `tests/test_dotenv_loading.py`.
2. **Imports** — `logging`, `uvicorn`, `FastAPI`, `CORSMiddleware`, `JSONResponse`,
   then the ten router modules.
3. **`app = FastAPI(title="JobRadar", lifespan=jobradar_lifespan)`** — runs logging
   setup + route dump on every boot (production *and* `TestClient`).
4. **Middleware** added in order: CORS, then `RequestLoggingMiddleware` (outermost,
   wraps CORS so preflight rejections and downstream 4xx are logged too).
5. **Global exception handler** (`@app.exception_handler(Exception)`) — logs
   `UNCAUGHT req_id=…` with full stack trace and returns `500 {"detail": "Internal
   Server Error", "request_id": …}`. Sets `X-Request-ID` on the response itself
   because `BaseHTTPMiddleware` does not always thread the exception-handler 500
   through its success-path header injection.
6. **Router mount table** — see §4.4.
7. **`GET /health`** + **`GET /health/env`** (single-boolean GITHUB_TOKEN diagnostic).
8. **`__main__`: `uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)`**.

### 5.2 Logging + observability (`backend/utils/logging.py`)

| Helper | Purpose |
|---|---|
| `setup_logging(level=None)` | Idempotent root-logger config. Honours `LOG_LEVEL` env var (DEBUG/INFO/WARNING/ERROR/CRITICAL). Replaces — does not stack — root handlers so pytest captures don't double-print. |
| `RequestLoggingMiddleware` | Stamps `request.state.request_id = uuid4().hex[:8]`; measures elapsed `time.monotonic`; logs `req_id=… <METHOD> <path> -> <status> (<duration>s) client=<host:port>` on success; logs the same line at UNCAUGHT on exception with the stack trace via `self._log.exception(...)`. Injects `X-Request-ID` into every successful response. |
| `dump_routes(app)` | Reads `app.openapi()["paths"]` (not `app.routes` — this OpenAPI introspection is necessary because FastAPI stores `include_router` results in private `_IncludedRouter` wrappers that have no `.path`/`.methods`/`.routes`). Filters out `/openapi.json`, `/docs*`, `/redoc` (tooling). Writes a header line + one `METHOD path` line per route, sorted, with HEAD duplicates stripped. |
| `jobradar_lifespan(app)` | `@asynccontextmanager` that runs `setup_logging` + `dump_routes` on startup and a "shutting down" log on teardown. |

Three module-level loggers (`jobradar.request`, `jobradar.startup`, `jobradar.error`)
are the explicit contract for emitting structured events. Tests attach capture handlers
to these names directly.

### 5.3 Middleware stack (outermost → innermost)

1. `RequestLoggingMiddleware` — access log + `X-Request-ID`
2. `CORSMiddleware` — allows `http://localhost:3000`, `http://127.0.0.1:3000`
3. FastAPI route handlers
4. FastAPI exception middleware (catches `Exception` → routes to our handler)

### 5.4 Routers — every mounted endpoint

All routers are mounted under `/api/<domain>`; tags show in `/docs`.

| Prefix | Tag | Methods | Endpoints |
|---|---|---|---|
| `/health` | — | GET | `/health` (liveness), `/health/env` (single-bool GITHUB_TOKEN diagnostic) |
| `/api/dashboard` | Dashboard | GET | `/api/dashboard/` (placeholder tiles) |
| `/api/scan` | Scan jobs | POST | `/api/scan/{funding\|ngos\|remote\|oss\|boards\|/}`, each accepting `delta_hours`, `limit`, plus domain-specific query params (`languages` for OSS, `boards` filter for `/boards`) |
| `/api/outreach` | Outreach | POST, GET | `POST /api/outreach/generate` (body `{company_id, type, user_context}`, types are `email\|twitter_dm\|linkedin`); `GET /api/outreach/{company_id}` (in-memory keyed list, newest first) |
| `/api/companies` | Companies | GET, PATCH | `GET /api/companies` (filters: `category`, `source`, `status`, `search`, `limit`, `offset`); `GET /api/companies/stats`; `GET /api/companies/{id}`; `PATCH /api/companies/{id}/status` (pre-application CRM statuses) |
| `/api/pipeline` | Pipeline | GET, PUT, POST | `POST /api/pipeline/run` (kick LangGraph 4-domain), `GET /api/pipeline/status`, `GET /api/pipeline/discover` (boards-only with `delta_hours=168`), `GET /api/pipeline/schedule`, `PUT /api/pipeline/schedule` (Literal `[1, 2, 4, 6, 12, 24]`), `GET /api/pipeline/stats` |
| `/api/applications` | Applications | GET, PATCH | `GET /api/applications?status=…&page_size=…`, `PATCH /api/applications/{id}/status` (post-application statuses: `submitted\|interview\|rejected\|offer\|ghosted`); `notes` only mutates when the payload carries the field |
| `/api/jobs` | Jobs | GET, POST | `GET /api/jobs?status=…&page_size=…`, `GET /api/jobs/pending-count`, `POST /api/jobs/{id}/approve`, `POST /api/jobs/{id}/reject`. Pre-application statuses: `in_review\|approved\|rejected\|applied\|flagged` |
| `/api/qa-bank` | QA bank | GET, POST, PATCH, DELETE | `GET /api/qa-bank` (sorted by `times_used`), `POST /api/qa-bank` (derives `answer_type` from length: ≤120 chars → `short_text`, else `long_text`; lowercases `question_pattern`), `PATCH /api/qa-bank/{id}` (re-derives `answer_type` on `answer` change; whitespace-only string normalizes to `null`), `DELETE /api/qa-bank/{id}` returns the deleted record |
| `/api/resumes` | Resumes | GET, POST, PATCH, DELETE | `GET /api/resumes` (`{resumes, total}`), `POST /api/resumes` (multipart: `file`, `tags`, `is_default`; 10 MiB cap → `413`), `PATCH /api/resumes/{id}` (tags normalized: trim/dedup/drop-blanks; single-default invariant on `is_default=True`), `DELETE /api/resumes/{id}` (204), `GET /api/resumes/{id}/download` (raw `Response` with `Content-Disposition`; seeded metadata without bytes → `410`) |
| `/api/settings` | Settings | GET, PATCH | Singleton `Preferences` (`target_roles`, `review_window_hours ∈ [0.5, 48]`, `job_fit_threshold ∈ [0, 1]`, `send_followup_emails`); PATCH normalizes `target_roles` (trim/dedup/drop-blanks, case-sensitive) and reflects server-side cleanup back through React Query's `setQueryData` |

---

## 6. Backend — Pipeline layer

### 6.1 LangGraph (`backend/pipeline/graph.py`)

A `StateGraph[PipelineState]` (`TypedDict` from `models/graph_state.py`) composes the
**four non-board domains in parallel**:

```
START ──> funding ─┐
START ──> remote  ─┼──> merge ──> END
START ──> ngos    ─┤
START ──> oss     ─┘
```

Each node writes its own `list[dict]` into the state (`funding`, `remote`, `ngos`,
`oss`). The `merge` node (`pipeline/nodes/merge.py`) takes all four buckets and
produces a `res` list sorted newest-first within the merged bundle. The compiled
graph is exported as `scan_pipeline`; `routes.pipeline.run_pipeline` calls
`scan_pipeline.invoke({"input": "api"})` and returns per-domain counts and payloads.

`delta_hours` is hard-coded per node (24h funding/remote, 72h ngos, 168h oss) — these
match the cadence that operators want; runtime override is intentionally not exposed
yet (would multiply state shape).

### 6.2 Job-board runner (`backend/pipeline/nodes/jobs_boards/runner.py`)

The boards runner is **separate** from the LangGraph because it uses a much larger
`delta_hours` window (typically 168h — a week) and runs on an hourly schedule. It is
threaded (`ThreadPoolExecutor(max_workers=8)`) so a slow org doesn't block the rest.

State is persisted to JSON files under `backend/data/`:

| File | Purpose |
|---|---|
| `ashby_companies.json` / `lever_companies.json` / `greenhouse_companies.json` | Slug indexes for each ATS board |
| `<board>_missing_orgs.json` | Orgs benched after `MISSING_THRESHOLD = 3` consecutive `404`/`410` failures |
| `seen.json` | Job IDs we've already delivered (avoid duplicates across runs) |
| `last_run.json` | ISO timestamp of the previous run + per-org most-recent post |
| `missing_failures.json` | Per-board failure streak counters (used for the threshold above) |
| `verified_org_targets.json`, `verification_progress.json`, `missing_failures.json` | Operator-curated verification context |

Robustness:

- `MISSING_THRESHOLD = 3` consecutive missing results before benching — a transient
  404 (maintenance / rate-limit) doesn't permanently drop coverage.
- A successful fetch `pop`s the slug from the failure streak and `add`s it to
  `recovered[board]` so it gets unbenched.
- Per-org errors (`httpx.TimeoutException`, scraper exception) are logged but never
  penalise the failure counter.

The runner is exposed via `routes.scanner.run_boards()` (synchronous,
`POST /api/scan/boards`) — same code path, different cadence.

---

## 7. Data stores

### 7.1 In-memory CRUD stores (the demo)

Every demo router uses the **same pattern** — a module-level `_FOO_DB: dict[str, dict]`
seeded from a deep-copied `_SEED_RECORDS` list at import time. Reset seams:

| Router | State key | Reset helper |
|---|---|---|
| `companies` | `_COMPANIES_DB` | `_seed()` (deep-copies seed list, clears + re-inserts) |
| `pipeline` | `_PIPELINE_STATE` | `_reset_state()` (mutates in place so importers keep working) |
| `settings` | `_PREFS_STATE` | `_reset_prefs()` (= `Preferences().model_dump()`) |
| `jobs` | `_JOBS_DB` | `_seed()` |
| `applications` | `_APPLICATIONS_DB` | `_seed()` |
| `qa-bank` | `_QA_DB` | `_seed()` |
| `resumes` | `_RESUMES_DB` + `_RESUME_BYTES` | `_seed()` (clears bytes too) |
| `outreach` | `_MESSAGES_DB: dict[company_id, list]` | Implicit via test setUp |

Tests call the reset helper from `setUp` so PATCH mutations between tests don't leak
across cases. The dict-mutation idiom (vs replace-the-reference) is what keeps router
references valid after a reset.

### 7.2 JSON-on-disk under `backend/data/`

The board runner's persistent state (see §6.2 table). Not currently consumed by any
demo router; operator-only inspection point for debugging the scraper.

### 7.3 Persistence layer (Postgres / Supabase)

The persistent storage layer lives under `backend/db/` and is managed by Alembic.
This branch delivers the **schema + migrations only** — the FastAPI routes still
use their in-memory `_FOO_DB` dicts so the demo keeps running without change.
The eventual route rewrite is mechanical: every column mirrors the corresponding
Pydantic model 1-for-1, so swapping `_FOO_DB[id]` for `session.execute(...)` is a
shape-preserving translation.

**Files**

| Path | Purpose |
|---|---|
| `backend/db/__init__.py` | Package marker; routes import from here once the rewrite lands. |
| `backend/db/models.py` | SQLAlchemy 2 declarative `Base` + 16 mapped classes + the 10 Postgres enum registries. Single source of truth for the schema. |
| `backend/alembic.ini` | Alembic config — script_location = `db/migrations`, URL overridden from `DATABASE_URL` at runtime. |
| `backend/db/migrations/env.py` | Async Alembic environment (engine, connection pool, autogenerate metadata binding). |
| `backend/db/migrations/script.py.mako` | Standard mako template. |
| `backend/db/migrations/versions/0001_initial_schema.py` | Single initial migration — creates the full schema in one shot. |

**Tables**

| # | Table | Purpose | Grain |
|---|---|---|---|
| 1 | `companies` | User-facing CRM spine. Matches `routes/companies.Company` 1-for-1. | 1 row per opportunity (any of 5 scanner categories). |
| 2 | `raw_scrapes_funding` | Scanner-specific landing table. | 1 row per scrape. |
| 3 | `raw_scrapes_remote` | Scanner-specific landing table. | 1 row per scrape. |
| 4 | `raw_scrapes_ngos` | Scanner-specific landing table. | 1 row per scrape. |
| 5 | `raw_scrapes_oss` | Scanner-specific landing table. | 1 row per scrape. |
| 6 | `raw_scrapes_boards` | Scanner-specific landing table. | 1 row per scrape. |
| 7 | `scanner_runs` | Per-invocation audit trail (started_at / finished_at / items_found / errors). | 1 row per LangGraph / boards runner invocation. |
| 8 | `jobs` | Pre-apply review queue. Matches `routes/jobs.Job`. | 1 row per job in the queue. |
| 9 | `applications` | Post-apply tracker. Matches `routes/applications.Application`. | 1 row per application. |
| 10 | `qa_bank_entries` | Q&A bank. Matches `routes/qa_bank.QAEntry`. | 1 row per question pattern. |
| 11 | `resumes` | Resume metadata only — bytes live in Supabase Storage, not in Postgres. | 1 row per resume (single-default invariant via partial unique index). |
| 12 | `outreach_messages` | Generated outreach. Matches `routes/outreach.OutreachMessage`. | 1 row per generated message. |
| 13 | `preferences` | Singleton user preferences (1-row enforced by `CHECK id = 1`). | 1 row total. |
| 14 | `pipeline_status` | Singleton LangGraph run state. | 1 row total. |
| 15 | `board_seen_jobs` | Replaces `backend/data/seen.json` — hourly ATS dedupe atomically. | 1 row per seen job id hash. |
| 16 | `ats_discovered_orgs` | Replaces `backend/data/<board>_missing_orgs.json` — tracks consecutive failures for the `MISSING_THRESHOLD = 3` benching rule. | 1 row per (board, slug). |

**Enum types** (10 Postgres-native enums, mirroring the `Literal[...]` Pydantic
types in `routes/*`):

`company_category, company_status, job_status, application_status,
qa_answer_type, outreach_type, scanner_kind, ats_board, ats_org_status,
pipeline_state` — all created in the enum section of `0001_initial_schema.py`
before any table that references them.

**Indexing strategy**

- **Composite indexes** match the existing read paths: `(category, status,
  published_at DESC)` on `companies` powers the React CompanyFeed; `(status,
  created_at DESC)` on `jobs` powers the per-status filter tabs; `(company_id,
  created_at DESC)` on `outreach_messages` powers the per-company history.
- **Partial indexes** keep working-set fragments cheap:
  `(review_deadline ASC) WHERE status = 'in_review'` on `jobs` keeps the
  pending-count query sub-millisecond even after a year of history;
  `UNIQUE INDEX ... WHERE is_default = true` on `resumes` enforces the
  single-default invariant in the engine (no application-level demote).
- **GIN-ready fields** (`tags`, `hiring_signals`, `target_roles`) are stored
  as `TEXT[]` rather than JSONB so the simple-string-tuple lookup
  (`ANY(tags)` / `@>` indexing) stays fast. Indexes can be added later
  via `CREATE INDEX ... USING GIN (tags)` without a schema migration.

**Schema decisions worth knowing**

- **UUID primary keys**, generated by Postgres via `gen_random_uuid()` (the
  migration enables `pgcrypto` if missing). Python code does not specify
  ids on insert — the DB handles it.
- **Resume bytes are NOT in Postgres.** Storing 10 MiB blobs in `bytea`
  bloats the buffer cache and pg_dump backups; the schema keeps an
  `storage_path` reference and the eventual route migrates `POST
  /api/resumes` to upload to Supabase Storage. Seeded metadata without
  bytes still validates against the schema.
- **No soft-delete columns.** JobRadar already expresses lifecycle via
  status enums (`dismissed`, `rejected`, `ghosted`). Adding a global
  `deleted_at` would litter every read query for zero benefit.
- **Singleton tables use `CHECK (id = 1)`** rather than application-level
  guards — cheaper and more reliable.
- **Raw landing tables use `ON DELETE SET NULL`** on the `company_id` FK
  so a `companies` row demotion never cascades scratch data away.

**Migration workflow**

```bash
# Apply via Alembic (docker compose injects DATABASE_URL automatically)
cd backend && alembic upgrade head

# Roll back the most recent migration
cd backend && alembic downgrade -1

# Show current revision against an existing DB (no mutations)
cd backend && alembic current

# Diff declarative models against live DB (would emit a new revision file)
cd backend && alembic revision --autogenerate -m "describe the change"

# Generate vanilla SQL without applying (good for Supabase SQL-editor review)
cd backend && alembic upgrade head --sql
```

The single initial migration is self-contained — fresh databases apply it
in one shot. Future changes piggy-back on `down_revision =
"0001_initial_schema"` and follow the standard Alembic revision workflow.

**Supabase CLI mirror**

If you prefer the Supabase CLI workflow (`supabase db push` / `supabase db
reset`), the schema is also published at `<repo-root>/supabase/migrations/
<timestamp>_initial_schema.sql`. The file is a flat re-statement of the
Alembic `upgrade()` so `supabase db diff` will be empty after applying.
Any new schema change goes in BOTH a new Alembic revision file AND a new
flat SQL file under `supabase/migrations/` with the next higher
`YYYYMMDDHHMMSS_*` timestamp.

**Supabase pooler quirks** are handled in
`backend/db/migrations/env.py::_resolve_database_url`. When
`DATABASE_URL` host contains `pooler.supabase.com` or port `6543`, the
helper appends `prepared_statement_cache_size=0` so asyncpg doesn't try
to reuse prepared statements across pgBouncer-rotated connections.

**Authentication / Storage**

* Resumes are stored in the Supabase Storage `resumes` bucket (private).
  Bytes are uploaded via `backend.storage.supabase.upload_resume_bytes`,
  which wraps the synchronous `supabase`-py SDK in
  `fastapi.concurrency.run_in_threadpool` so the FastAPI event loop is
  never blocked.
* The service-role key is the **only** Supabase key the backend uses —
  never expose `SUPABASE_SERVICE_ROLE_KEY` to the frontend. The frontend
  continues to talk to the FastAPI proxy exclusively.
* RLS is intentionally **off** because JobRadar is a single-user app
  and the backend is the sole owner. `auth.users` is not joined because
  none of the routes filter on `auth.uid()` today. When multi-user
  becomes a goal, both layers light up together (add `user_id` to every
  owned table + enable RLS + write `auth.uid()`-based policies).

---

## 8. Frontend

### 8.1 Stack

- **Vite 8** dev server + HMR, port `3000`, **`/api` proxy → `http://localhost:8000`**
  (defined in `vite.config.js`). React Router v7 + TanStack Query v5 + axios.
- **React 19** with `StrictMode` (catches double-effect bugs).
- **Tailwind CSS 4** via `@tailwindcss/vite` plugin (no PostCSS config).
- **Vitest** + jsdom for component tests; `@testing-library/react` +
  `@testing-library/user-event` for interaction; `setupFiles: ['./src/test-setup.js']`
  wires `@testing-library/jest-dom`.

### 8.2 Routing (`src/App.jsx`)

```jsx
<BrowserRouter>
  <CategoryProvider>     // Cross-component UI state (selected category)
    <Shell>
      <Navbar />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/company/:id" element={<CompanyDetail />} />
        <Route path="/jobs" element={<JobsReview />} />
        <Route path="/applications" element={<ApplicationTracker />} />
        <Route path="/qa-bank" element={<QABank />} />
      </Routes>
    </Shell>
  </CategoryProvider>
</BrowserRouter>
```

Modal overlays (`ResumesModal`, `PreferencesModal`, `SchedulerModal`) render *inside*
the navbar/page tree so `category` state propagates without explicit prop-drilling.

### 8.3 Server state (TanStack Query)

Single `QueryClient` with `defaultOptions.queries = { retry: 1, refetchOnWindowFocus:
false }`. Hooks in `src/hooks/` wrap each fetch + mutate pair:

| Hook | Reads / writes |
|---|---|
| `useCompanies()` | `['companies']` query + PATCH status mutation |
| `useResumes()` | `['resumes']` query + upload/PATCH/DELETE mutations |
| `usePreferences()` | `['preferences']` query; PATCH `onSuccess` writes the normalised response back via `setQueryData` then `invalidateQueries` for full reconciliation |
| `useJobs(filters)` | `['jobs', filters]`; poll every 60 s |
| `usePendingCount()` | `['jobs', 'pending-count']`; poll every 30 s for the navbar badge |
| `useApplications(filters)` | `['applications', filters]`; poll every 60 s |
| `useQABank()` | `['qa-bank']`; staleTime 60 s |
| `useOutreach()` | Pascal `generate_outreach` mutation |

### 8.4 Axios (`src/api/`)

A single axios instance `api` with `baseURL = ${VITE_API_URL}/api` and a default
`Content-Type: application/json` header. Files in `src/api/` export typed wrapper
functions (`fetchJobs`, `approveJob`, etc.) — these mirror the openapi one-for-one so
the React surface never sees `axios` types.

Two subtle behaviours baked in:

- **`uploadResume`** sets `headers: { 'Content-Type': undefined }` to disable the
  instance default so axios's FormData branch emits the proper
  `Content-Type: multipart/form-data; boundary=…` (the comment in `api/resumes.js`
  documents the bug naïveté that breaks otherwise).
- **`buildDownloadUrl`** returns a *string* (not an axios promise) for the resume
  download — the React ResumesModal uses it as an anchor `href` so the browser
  handles the download natively with the correct `Content-Disposition` filename.

### 8.5 Components

- **`Navbar`** — top nav + per-category pages + modal triggers; subscribes to
  `useCategory()`.
- **`Dashboard`, `CompanyDetail`, `JobsReview`, `ApplicationTracker`, `QABank`** —
  page-level routes (`pages/`).
- **`CompanyFeed`, `CompanyCard`, `OutreachPanel`, `StatusTracker`, `ScheduleControl`**
  — feature-level components in `components/`.
- **`Modal` (base)** + **`ResumesModal`, `PreferencesModal`, `SchedulerModal`** —
  controlled overlays.

---

## 9. Job lifecycle — the canonical story

```
                                   ┌──────────────────────────────┐
1. ATS discovery (24 h)            │ external: Serper / Playwright│
                                   │ outcome: (board, slug) rows  │
                                   └─────────────────┬────────────┘
                                                     │
                                                     ▼
┌────────────────────────────────────────────────────────────────┐
│ 2. Hourly scraper                                                │
│   board fetcher (httpx) → per-org jobs (with `since` cutoff)     │
│   filter_roles → LLM scoring → Job(in_review, review_deadline)   │
└───────────────────────────────────────────┬────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────┐
│ 3. Operator                                                       │
│   /jobs → click Approve                                          │
│   POST /jobs/{id}/approve → status flips to "approved"          │
│   apply-worker poll (Redis BRPOP) → Playwright opens URL,        │
│     matches Q&A bank (rapidfuzz → LiteLLM semantic fallback),   │
│     submits form, screenshots confirmation page                  │
│   Application(submitted) row inserted                            │
└───────────────────────────────────────────┬────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────┐
│ 4. Recruiter reply (days later)                                   │
│   gmail_poll (15 m) → fetches threads tagged "job-applications"  │
│   LiteLLM classifier → interview / rejected / other              │
│   PATCH Application.status                                        │
└────────────────────────────────────────────────────────────────┘
```

The demo today fully exercises steps 2 (manual `POST /api/scan/boards`),
`POST /api/pipeline/run` (LangGraph 3), manual approval, and
`PATCH /api/applications/{id}/status`. Steps 1 (ATS discovery) and 4 (Gmail poller)
are documented intent; their full Docker pipeline lives in the README at the repo
root.

---

## 10. External resources used

| Resource | Consumed by | Notes |
|---|---|---|
| **GitHub Trending** (`github.com/trending/{lang}`) | `pipeline/nodes/oss/...` | BS4 scraper, uncached, re-fires every tick. |
| **GitHub Search API** (`label:"good first issue"`) | same | Cached behind `@lru_cache(maxsize=32)` keyed on `(language, day, per_page)`. **Keep `per_page=15` frozen** — any change busts the cache and re-burns the 5,000/hr quota. |
| **LiteLLM (NVIDIA NIM primary, Groq fallback)** | Outreach (`outreach._pick_qa`/`_pick_resume`), LangGraph `scan_pipeline` fit-scoring, gmail classifier, apply-worker's qa-matcher | Primary → fallback chain lives in the worker; the backend exposes `LLM_PROVIDER`/`LLM_MODEL`/`LLM_API_KEY` env vars. |
| **Ashby / Lever / Greenhouse public APIs** | `pipeline/nodes/jobs_boards/{ashby,lever,greenhouse}.py` | All three are anonymous JSON; only GitHub needs auth for an enriched quota. |
| **Serper (Google Search)** | ATS discovery | Optional; falls back to Playwright when `SERPER_API_KEY` is unset. |
| **Gmail API** | gmail-poll task (planned) | Read-only; uses OAuth. |
| **Postgres 16** | `route` DB-backed stores | Demo uses in-memory; production reads from this. |
| **Redis 7** | `apply_queue` (BRPOP) + scheduler overrides | Bridges backend and apply-worker. |
| **Playwright** | apply-worker (form submission + screenshots) | Not present in the demo FastAPI process. |
| **BeautifulSoup4** | OSS / funding-page scraping | Static HTML parsing. |

---

## 11. Layers + cross-cutting concerns

### 11.1 Frontend ↔ backend contract stability

Every API wrapper in `src/api/` is a one-for-one mirror of an openapi operation. To add
a new endpoint:

1. Add the Pydantic models + route + (optional) `response_model` in `backend/routes/X.py`.
2. Mount it in `backend/main.py` `app.include_router(...)`.
3. Add a wrapper function in `src/api/X.js` (or extend an existing file).
4. Add a hook in `src/hooks/useX.js` that wraps it with TanStack Query.
5. Wire it into a page or modal component.

The reverse contract also holds: if the React component expects an envelope shape
(`{jobs, total}` vs a flat list), the backend `response_model=…` makes that contract
visible in `/docs`.

### 11.2 Observability

- **Single source of truth:** `backend/utils/logging.py`. Module-level loggers are
  `jobradar.request` / `jobradar.startup` / `jobradar.error`. Every router imports only
  these and never creates ad-hoc loggers.
- **Per-request `X-Request-ID`** (8-char hex `uuid4` prefix) — emitted by the
  middleware on every response, in the access log line, AND in the 500 body. Operators
  can grep the log by that ID.
- **`dump_routes(app)`** runs on startup and prints one line per (method, path). This
  is *the* canary for "did my router actually mount?" — the user-facing symptom of
  "I added a router but `/api/foo/*` 404s" is invisible otherwise.

### 11.3 Validation

- **Path/query constraints** declared in Pydantic (`Path(min_length=1, max_length=64)`,
  `Query(ge=1, le=200)`) → 422 on out-of-range.
- **`Literal[...]` enums** for status / category / type / answer_type / ats_type —
  expand the set on both sides together (frontend `STATUS_COLORS` + backend Literal).
- **Content size**: `MAX_BYTES = 10 * 1024 * 1024` per resume upload, checked in
  `routes/resumes.upload_resume` before storing → 413 Payload Too Large (the React UI
  already has a friendly 413 branch in `ResumesModal`).
- **Concurrency**: `routes.pipeline.run_pipeline` + `discover` set `_state = "running"`
  up front and reset to `"idle"` in `finally`; subsequent launches raise `HTTPException
  409`, "pipeline is already running".

### 11.4 Error semantics

- `HTTPException(404)` for missing IDs, `HTTPException(409)` for concurrent state,
  `HTTPException(410)` for "bucket cleared" (download on seeded-metadata), `413` for
  payload size, `422` for Pydantic validation; caught by FastAPI's default middleware
  and rendered as `{"detail": "..."}` (or the custom envelope if the route raised with
  `detail=...`).
- Anything else routes to `app.exception_handler(Exception)` → 500 with `request_id`,
  stack trace on `jobradar.error`.

---

## 12. Testing

### 12.1 Conventions

`python -m unittest discover tests -v` from `backend/` (pytest is not in `pyproject.toml`
on purpose — keeps the project installer-lean). Every test file mirrors the project's
"in-memory seeded store + test-reset seam + TestClient per test" idiom:

```python
class _XTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _seed()                   # reset module-level state
        self.client = TestClient(app)
```

### 12.2 Coverage (177 tests in the demo)

| Test file | Tests | What it pins |
|---|---|---|
| `test_dotenv_loading.py` | multiple | The 3-tier env precedence (shell > backend/.env > root/.env). |
| `test_outreach.py` | covers `outreach.py` routes | Selection (Jaccard) of resume + QA; render of email/twitter_dm/linkedin; ordering of `GET /outreach/{company_id}`. |
| `test_companies.py` | covers `companies.py` | Filter composition; status patch idempotency; openapi dump. |
| `test_pipeline.py` | covers `pipeline.py` | Concurrency 409 guard; schedule literal validation; run envelope + counts; mock-invoked tests. |
| `test_jobs.py` | covers `jobs.py` | List + filter + page_size; pending-count; approve/reject + deadline clear; sort order pinning. |
| `test_applications.py` | covers `applications.py` | Filter, page_size, full `[a_6, a_1, a_2, a_5, a_3, a_4]` sort order, status patching (incl. notes preservation). |
| `test_qa_bank.py` | covers `qa_bank.py` | Sort by `times_used`, `SHORT_TEXT_LIMIT = 120` boundary, POST + PATCH whitespace normalisation (via shared `_clean_answer` helper). |
| `test_resumes.py` | covers `resumes.py` | Upload 10 MiB cap → 413; singles-default invariant; PATCH tag normalisation; download for uploaded bytes + 410 path on seed metadata. |
| `test_settings.py` | covers `settings.py` | Defaults mirror `usePreferences.DEFAULT_PREFERENCES` exactly; PATCH round-trips on every field; bounds → 422. |
| `test_request_logging.py` | covers `utils/logging.py` + global `Exception` handler | `X-Request-ID` 8-char hex + uniqueness across requests; access log content for 200/404; dump-routes shows mounted routes; uncaught 500 with matching body `request_id` + stack-trace log + `X-Request-ID` header. |
| `test_oss.py`, `test_scanner_hardening.py`, `test_job_board_runner.py`, `test_domain_runners.py`, `test_missing_org_verifier.py` | scraper / runner unit tests | Pluggable httpx Mocks; missing-org benching at `MISSING_THRESHOLD`; etc. |

(Three pre-existing failures in `test_domain_runners.py` are unrelated to demo
crates; reproduced on stashed clean HEAD.)

### 12.3 Frontend tests (`npm test`)

Vitest + jsdom. Convention: co-located `X.test.jsx` next to `X.jsx`, OR explicit
`__tests__/` directory next to the unit under test. Two existing patterns:

- **Hook-mock** (`OutreachPanel.test.jsx`) — `vi.mock('../../hooks/useOutreach')` to
  replace the hook entirely. Best for components that consume only the hook.
- **API-mock** (`ScheduleControl.test.jsx`, `client.test.js`) — `vi.mock('axios')` to
  replace the network boundary. Best for components that import api wrappers directly.

`X-Request-ID` assertion regression coverage is in `client.test.js` (axios-mock
verifies `triggerDiscovery` uses `axios.get`, not `axios.post`).

### 12.4 Test commands

```bash
# Backend — all 177 tests
cd backend && python -m unittest discover tests -v

# Backend — single file drill-down
cd backend && python -m unittest tests.test_qa_bank -v

# Frontend — vitest
cd frontend && npm test              # run once (CI shape)
cd frontend && npx vitest --reporter=verbose src/api/__tests__/client.test.js

# Frontend — lint
cd frontend && npm run lint
```

---

## 13. Development + environment variables

### 13.1 Environment precedence

`shell > backend/.env > repo-root/.env`. Both `.env` files are loaded with
`override=False`. **Edits to a `.env` are NOT picked up by `uvicorn --reload`** —
the loader runs at import time only. Bounce the process to apply.

### 13.2 Backend variables (in priority order)

| Var | Required? | Effect when missing |
|---|---|---|
| `DATABASE_URL` | yes (docker) | FastAPI storage layer fails to start; demo is unaffected (in-memory only). |
| `REDIS_URL` | yes (docker) | Scheduler queue is dormant; `/api/pipeline/*` no-ops. |
| `POSTGRES_PASSWORD` | yes (docker) | Dockerised Postgres bootstrap fails. |
| `LLM_PROVIDER` | yes | Fit scoring is fail-closed; scrapers still run unranked. |
| `LLM_API_KEY` (or `GROQ_API_KEY` / `NVIDIA_API_KEY`) | yes | LLM ranker cannot score; jobs land in `in_review` unranked. |
| `GITHUB_TOKEN` | optional but recommended | OSS good-first-issues: 60 req/hr anonymous → 5,000 req/hr authenticated. **Read once at import** — restart to rotate. |
| `LOG_LEVEL` | optional (default `INFO`) | One of DEBUG/INFO/WARNING/ERROR/CRITICAL. |

### 13.3 Frontend variables

| Var | Default | Effect |
|---|---|---|
| `VITE_API_URL` | `''` | axios `baseURL` becomes `${VITE_API_URL}/api`. Docker compose sets `http://localhost:8000`. |

---

## 14. File map — where to find what

| Want to understand… | File |
|---|---|
| FastAPI app construction, middleware, lifespan | `backend/main.py` |
| Custom request logging + lifespan helper | `backend/utils/logging.py` |
| OpenAPI-driven route enumeration via `app.openapi()["paths"]` | `backend/utils/logging.py::_iter_routes` |
| Global 500 handler, request-id propagation | `backend/main.py::_unhandled_exception_handler` |
| LangGraph assembly + per-domain nodes | `backend/pipeline/graph.py` |
| LangGraph state shape | `backend/models/graph_state.py` |
| Boards-runner parallel fans, missing-org benching | `backend/pipeline/nodes/jobs_boards/runner.py` |
| Cross-domain merge (per-domain buckets → sorted res list) | `backend/pipeline/nodes/merge.py` |
| In-memory CRUD + `_seed()` test seams | `backend/routes/{companies,jobs,applications,qa_bank,resumes,settings,pipeline}.py` |
| ATS form submission + Playwright (out-of-process worker) | `backend/apply_worker/` (currently planned; only the demo in-memory stores exist for routes today) |
| Frontend ↔ backend wire shape (the source of truth for one-for-one mirroring) | `frontend/src/api/`, `frontend/src/hooks/` |
| React Router + QueryClient + CategoryProvider wiring | `frontend/src/App.jsx` |
| Vite dev server, `/api` proxy, Vitest config | `frontend/vite.config.js` |

---

## 15. Where this doc lives in the lifecycle

- Edit this file alongside any **architectural change** (new router, new layer, new
  external resource).
- The micro-READMEs (`README.md`, `backend/README.md`, `frontend/README.md`) cover
  install / quickstart; this one covers *understanding*. Point new contributors here
  first when onboarding.
- If a router's wire shape changes (new field, new status enum value, new
  normalisation rule), update both the route's module docstring AND this doc's
  endpoint table at the same time — they are the two documentation sites the React
  side and the operator side read respectively.
