# JobRadar

Self-hosted, single-user AI job-hunting platform. Discovers startup job boards
on a schedule, scores postings against your profile via LLM, semi-automates
applications through a hybrid review queue, submits forms with Playwright.

The platform is a thin **API + DB** today — the heavy lifting (boards scan,
auto-apply) runs as **GitHub Actions cron jobs** hitting the same Supabase
Postgres, so there is no idle compute and no Redis queue in the box.

## Stack

| Layer | Tech |
|---|---|
| API | FastAPI, SQLAlchemy 2 async, Alembic |
| Storage | Supabase Postgres 16 + Supabase Storage (resumes / screenshots) |
| LLM gateway | Official `openai` Python SDK — NVIDIA NIM (primary) → NVIDIA NIM (optional 2nd key) → Groq (fallback). Shared token bucket sized at `len(nvidia_keys) * 40 RPM`. |
| Form submission | Playwright (Chromium), called from a separate GHA cron process |
| Frontend | Vite + React 19 + TanStack Query + Tailwind 4 |
| Scheduling | GitHub Actions cron (boards-scan + apply-worker); FastAPI itself has **no** background scheduler |

## Architecture

Three services in production:

```
┌────────────────────────────────────────────────────────────────┐
│  browser  ───►  frontend (Vite + React)  :3000                 │
│                                                                 │
│  ┌──── proxy /api → backend :8000 ────┐                        │
│  ▼                                   ▼                         │
│ Render/Compose: backend (FastAPI)  ───►  Supabase Postgres    │
│                                                  ▲              │
│  GitHub Actions cron (the engine):               │              │
│   ├─ boards-scan (active: hourly / dormant: daily│ / cleanup: │
│   │   weekly)  ── fetch Ashby/Lever/Greenhouse ───┤            │
│   │   LLM-score against profile.yml → INSERT `jobs(approved)`  │
│   ├─ apply-worker (hourly drain, max 1h tick)                  │
│       SELECT … FOR UPDATE SKIP LOCKED → Playwright →           │
│       INSERT `applications`, audit row, screen-shot upload     │
│   └─ Optional: `enrich_org_profiles.py` (one-time LLM          │
│       classifier) writes per-org skip lists the boards runner  │
│       consults when BOARDS_USE_ENRICHED_PROFILES=1.            │
└────────────────────────────────────────────────────────────────┘
```

- **FastAPI is the read/write surface** — every dashboard mutation hits
  `/api/*` and the DB. There is no APScheduler; the heavy loops live in
  GitHub Actions so the prod box is just a stateless proxy.
- **The boards runner** (`backend/pipeline/nodes/jobs_boards/runner.py`)
  runs in the GHA worker: 8-way thread-pool fetch over the per-board org
  list, heuristic role prefilter (`utils/filters.py`), then LLM scoring
  with a profile-aware prompt. Above-threshold jobs land in the `jobs`
  table with `status='approved'` directly — the LLM scoring IS the
  approval decision. Below-threshold jobs are dropped before the DB
  write.
- **The apply worker** (`backend/apply_worker/main.py`) drains
  `status='approved'` rows under `SELECT … FOR UPDATE SKIP LOCKED`
  (multi-dyno-safe) hourly. Playwright fills the form using a
  rapidfuzz-first pass with a single batched LLM fallback
  (`apply_worker/qa_matcher.py` → `services/llm_client.LLMClient`)
  for ambiguous field labels, then screenshots and uploads via
  Supabase Storage. On form-fill failure it parks the row as
  `status='paused'` so the dashboard surfaces a "Paused" sub-list
  for operator intervention.
- **Profile context** is sourced from `config/profile.yml`, not the Q&A
  bank. The Q&A bank is reserved for the application form auto-fill.
  Upload a resume → `services/profile_service.py:extract_profile_from_resume`
  LLM-extracts a structured profile and writes it to
  `config/profile.yml` automatically.

## One job's lifecycle

1. **Discovery** — GHA `boards-scan` runs three cron tiers:
   - **active** (hourly, 1h lookback) — the warm path; sets
     `BOARDS_TIER=active` so the `scanner_runs` audit row is tagged.
   - **dormant** (daily 02:20 UTC, 24h lookback with a longer HTTP
     timeout so timed-out orgs get a re-attempt window).
   - **cleanup** (weekly Sunday 04:20 UTC) — a *different* script
     (`pipeline.nodes.jobs_boards.cleanup_missing_orgs --no-resume`,
     run as `python -m pipeline.nodes.jobs_boards.cleanup_missing_orgs`)
     re-probes the `data/<board>_missing_orgs.json` slugs against
     the three ATS boards in dry-run mode so a transient 404
     doesn't permanently bench an org. (Not to be confused with the
     one-time `scripts/enrich_org_profiles.py` LLM-classification
     pass — that's a separate, manually-run script that writes the
     `data/enriched/<board>/_skip_list.json` files consulted when
     `BOARDS_USE_ENRICHED_PROFILES=1`.)

   Each tier runs `pipeline.nodes.jobs_boards.runner.run_all`,
   heuristic-filters (clearance / sponsorship / seniority),
   threads the operator's profile.yml target-roles into the
   relevance check, then LLM-scores. Winners land in the `jobs`
   table as `status='approved'` directly.
2. **Review** — the React `JobBoard` page (route `/jobs`) shows the
   approved worker's queue with a `PendingReviewWidget` "Paused" sub-list.
   Operator can pause/resume individual rows.
3. **Auto-apply** — GHA `apply-worker` cron hourly ticks. Each tick:
   - `SELECT … WHERE status='approved' ORDER BY created_at ASC
     LIMIT 1 FOR UPDATE SKIP LOCKED` (race-safe across worker dynos).
   - Pick a resume via tag-overlap first, then LLM fallback
     (`services.llm_client.pick_best_resume`); park `paused` if neither.
   - Fill the form (Playwright) using Q&A-bank + resume.
   - On success: write `applications` row + audit + flip `approved → applied`.
4. **No email tracking yet** — v1 does not poll Gmail. A rec
   ruiter reply is surfaced via `PATCH /api/applications/{id}/status`
   manually.

## Scoring + the profile.yml contract

Profile context flows one way: `config/profile.yml` → LLM scoring prompt.
The rebust `services.profile_service.build_profile_summary` renders a
multi-section markdown block (target roles by fit level, narrative,
proof points, candidate identity, comp, location) that drives the
7-factor `SYSTEM_PROMPT` in `services/llm_client.py`.

Resolution order when scoring a job (highest priority first):
1. `--target-roles` CLI override on `scripts/boards_scan.py` (one-off
   runs REPLACE the profile's `target_roles` entirely). An **empty**
   `--target-roles=""` is *not* a destructive override — `_resolve_profile`
   treats an empty list as "operator didn't pass the flag" and falls
   back to the on-disk profile.
2. `config/profile.yml` (operator's own) or `config/profile.example.yml` fallback.
3. `TARGET_ROLES` env var (legacy cron scripts).
4. Empty → LLM prompt renders `(no profile configured)` and degrades
   gracefully (the 7-factor prompt still produces a sensible score).

## Board runner skip mechanisms

The boards runner consults two on-disk skip lists before each fetch:

- **LLM-classified skip list** (`data/enriched/<board>/_skip_list.json`,
  written by the one-time `scripts/enrich_org_profiles.py --board all`
  LLM pass) — gated by `BOARDS_USE_ENRICHED_PROFILES=1`. The script
  classifies every org as "dead for 6+ months", "sponsorship-blocked",
  or "confidently non-tech"; the runner drops those slugs from the
  cron. Set `=1` in the boards-scan GHA env *after* the one-time
  classification run; unset (the default) preserves current behavior.
- **Slow-org list** (`data/<board>_timeout_orgs.json`, written by the
  runner itself when a fetch times out twice in a row) — gated by
  `BOARDS_SKIP_TIMEOUTS=1`. The active tier sets this so the slow-org
  list doesn't burn the hourly budget; the daily dormant tier unsets
  it and uses a longer `BOARDS_HTTP_TIMEOUT=30` to re-attempt them.

Without either env var, current behavior is preserved bit-for-bit.

## Supabase setup (production target)

JobRadar's persistent state lives on Supabase: Postgres + Storage + project
secrets. Start there before running anything local.

1. **Create a project.** Sign up at [database.new](https://database.new).
2. **Get the connection string.** Settings → Database → Connection string → URI.
   Pick **Transaction pooler** (port `6543`). Use
   `postgresql+asyncpg://…` for `DATABASE_URL`; Alembic auto-appends
   `prepared_statement_cache_size=0` against the pooler URL.
3. **Get storage + auth keys.** Settings → API → `SUPABASE_URL`,
   `SUPABASE_SERVICE_ROLE_KEY`. **Service role is server-side only** —
   never expose to the React frontend.
4. **Apply the schema.** Both paths produce the same surface; keep them
   in sync when you add a column:

   ```bash
   # Path A — Alembic (matches the backend's declarative SQLAlchemy)
   cd backend && alembic upgrade head

   # Path B — Supabase CLI (mirrors the Alembic migration)
   supabase db push
   ```

   Schema lives at `backend/db/models.py` (declarative Alembic) and
   `supabase/migrations/*` (Supabase CLI mirror). The Storage bucket
   is auto-created by `supabase/migrations/20260101000000_storage_resumes_bucket.sql`.

5. **Optional:** install the [Supabase CLI](https://github.com/supabase/cli).

`docs/project-overview.md` §7.3 has the full table / enum / index design
rationale.

## Quickstart (local dev)

```bash
# Prereqs: Docker Compose, plus a Supabase project (see above) and an
# API key for at least one of NVIDIA / Groq.
docker compose up --build
```

`docker-compose.yml` is intentionally lean — only `backend` and `frontend`.
Redis + apply-worker are gone (the worker moved to GHA cron).

A `.env` at the repo root or `backend/.env` must include:

```env
DATABASE_URL=postgresql+asyncpg://postgres.PROJECT_REF:PASSWORD@…
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<your-service-role-secret>
GROQ_API_KEY=<your-key>      # OR NVIDIA_API_KEY — at least one LLM provider
```

Open once it's up:

- Frontend: <http://localhost:3000>
- API + Swagger: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

To run a boards scan locally (the piece that GHA runs on cron):

```bash
cd backend && python scripts/boards_scan.py --dry-run --limit 50
```

To run the apply worker locally (drains the `approved` queue):

```bash
cd backend && python scripts/apply_worker_tick.py --timeout-seconds 60
```

## Where to look

| Want to understand … | File |
|---|---|
| LLM scoring chain (NVIDIA → Groq) + scorer profile prompt | `backend/services/llm_client.py`, `backend/services/scoring_service.py` |
| Profile loader/saver + resume → profile LLM extraction | `backend/services/profile_service.py` |
| Job → Apply apply worker (Playwright + Q&A + resume picker) | `backend/apply_worker/` (`main.py`, `form_filler.py`, `qa_matcher.py`, `resume_picker.py`) |
| Boards runner (the GHA cron entry) | `backend/pipeline/nodes/jobs_boards/runner.py` |
| Boards-scan entry script + audit-row writer | `backend/scripts/boards_scan.py` |
| Org-enrichment one-time LLM pass (writes `_skip_list.json`) | `backend/scripts/enrich_org_profiles.py` |
| Weekly cleanup tier (re-probes missing-org slugs) | `backend/pipeline/nodes/jobs_boards/cleanup_missing_orgs.py` |
| DB schema (16 tables, audit trail, scanners audit) | `backend/db/models.py`, `backend/db/migrations/versions/0001_initial_schema.py`, `supabase/migrations/*.sql` |
| LangGraph 4-domain scanner (funding / remote / ngos / oss) | `backend/pipeline/graph.py`, `backend/pipeline/nodes/{funding,remote,ngos,oss}/runner.py` |
| Resume upload → automatic LLM profile extraction (BackgroundTask) | `backend/routes/resumes.py` + `backend/services/profile_service.py:_run_profile_extraction_after_upload`; manual retry endpoint: `POST /api/profile/regenerate` |
| Profile singleton read for the React UI | `GET /api/profile` → `backend/routes/profile.py` |
| FastAPI routes (companies, jobs, applications, qa-bank, …) | `backend/routes/` |
| Frontend ↔ backend wiring (axios + TanStack Query) | `frontend/src/api/`, `frontend/src/hooks/` |
| GHA cron configs + secret lint | `.github/workflows/{boards-scan,apply-worker}.yml` |
| Render Blueprint | `render.yaml` |
| Deep reference (every endpoint, every table, every test) | `docs/project-overview.md` |
| In-flight design specs + implementation plans (job-loop wiring, auto-apply) | `docs/superpowers/specs/`, `docs/superpowers/plans/` |

## Development

- Backend tests: `cd backend && pytest tests/ -q`
- Frontend tests: `cd frontend && npm test`
- Lint: `cd frontend && npm run lint`
- New migration: add an Alembic revision under
  `backend/db/migrations/versions/` **and** a flat SQL mirror under
  `supabase/migrations/` with a higher `YYYYMMDDHHMMSS_*.sql` timestamp.
  The two must stay byte-equivalent (see `docs/project-overview.md`).
- Live OSS smoke: `cd backend && python scripts/oss_smoke.py`
- Profile re-extraction: a resume upload automatically schedules an
  LLM profile extraction as a FastAPI `BackgroundTask` (writes
  `config/profile.yml` on completion). To manually re-run on an
  existing resume without re-uploading: `POST /api/profile/regenerate`
  (returns 202; reads the stored bytes and re-LLMs).

## License

Personal project — not currently licensed for redistribution.
