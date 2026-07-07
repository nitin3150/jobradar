# JobRadar — Backend

FastAPI service + LangGraph-backed domain scrapers the React/Vite frontend hits.

## Quickstart (standalone, no Docker)

```bash
# 1. Install deps (pyproject.toml is the source of truth; .venv is ignored)
cd backend
uv venv && source .venv/bin/activate
uv pip install -e .

# 2. Configure environment (see "Environment variables" below)
cp .env.example .env  # then fill in the secrets

# 3. Run
python main.py
# → uvicorn on http://localhost:8000  (Swagger at /docs, health at /health)
```

The full Dockerised pipeline (postgres + redis + apply-worker + frontend) is at the repo root: `docker compose up --build`. This README covers the **backend-only** path.

## Environment variables

All variables are read at process start (or via `load_dotenv()` if you wire one in
`main.py`). The docstring on each backend module is the source-of-truth per
variable; this README is the operator quick-reference.

| Variable          | Required? | Effect when missing                                              |
|-------------------|-----------|------------------------------------------------------------------|
| `DATABASE_URL`    | yes       | Backend fails to start (no `Company` / `Job` persistence).       |
| `REDIS_URL`       | yes       | Scheduler queue (`BRPOP`) is dormant; `/api/pipeline/*` no-ops.  |
| `POSTGRES_PASSWORD` | yes (docker) | Dockerised Postgres bootstrap ignores it without this.        |
| `LLM_PROVIDER`    | yes       | Fail-closed during `fit scoring`; scrapers still run.            |
| `LLM_API_KEY` (or `GROQ_API_KEY` / `NVIDIA_API_KEY`) | yes | LLM ranker cannot score; jobs land in `in_review` unranked. |
| `GITHUB_TOKEN`    | **optional** | OSS tab's good-first-issues call is rate-limited to **60 req/IP/hour**. With any personal access token, it jumps to **5,000 req/hour**. **Recommended for production.** |

### `GITHUB_TOKEN` in detail

> **TL;DR — `GITHUB_TOKEN=ghp_…` (or any classic / fine-grained PAT)
> flips the OSS scraper's GitHub API budget from 60 req/hour anonymous
> to 5,000 req/hour authenticated.** Read on only if your token isn't a
> classic PAT, or if you're tuning scheduler tick density.

The Open Source scraper (`pipeline/nodes/oss/github_issues.py`) calls
GitHub's Search API for `label:"good first issue"` issues, grouping
results per repo. GitHub's anonymous budget is **60 calls / IP / hour**,
which is fine for a one-off operator run but burns fast when the scraper
runs across multiple languages or from CI.

Set a personal access token to bump the budget:

```bash
# Fine-grained PAT or classic PAT — read:public_repo is enough
export GITHUB_TOKEN=ghp_…
```

|Wire shape on disk|Accepted?|Permission setup|
|---|---|---|
|Classic PAT (`ghp_…`)|✓|Tick the **`public_repo`** scope.|
|Fine-grained PAT (`github_pat_…`)|✓|Repository access = **"Public Repositories (read-only)"** *and* grant **Metadata: Read** + **Issues: Read**. (Fine-grained PATs have no `public_repo` scope.)|
|GitHub App installation token|`✗` (these need explicit header signing)|—|

The token is read **once at import** (module-level constant). To rotate,
restart the process — there is no hot-reload for `GITHUB_TOKEN`.

> **Caveat:** GitHub's Search API has a secondary rate limit of 30 req/min
> independent of the 5,000/hr ceiling, so a fast burst within a single
> minute is still throttled even with a token. `scan_oss` already loops
> languages serially (3 default languages → 6 HTTPS calls — 3
> `trending_scan` + 3 `gfi_scan` — in ~6-12 seconds per tick, well under
> the cap), so the default config is safe. **If you push languages past
> ~10**, sequence them across scheduler ticks — pick one language per
> N-tick window — instead of firing both scrapers × all languages at
> once. Caching caveat: the `lru_cache(maxsize=32)` is on `gfi_scan`
> only, so good-first-issues amortises to one call per `(language,
> day)`. **Keep `per_page=15` frozen** — `_cached_search` keys on
> `(language, day_key, per_page)`, so a future runner that passes a
> different `per_page` would silently bust the day-cache and re-burn the
> 5,000/hr budget for no extra coverage. `trending_scan` is uncached
> and re-fires every tick.

For guidance on creating a token, see GitHub's
[docs](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

## API surface

- `GET  /health` — liveness probe.
- `POST /api/scan/funding` — funding-news scraper
- `POST /api/scan/ngos` — NGO boards scraper
- `POST /api/scan/remote` — remote-jobs scraper
- `POST /api/scan/oss` — open-source scraper (trending + good-first-issues)
- `POST /api/scan/boards` — ATS board scraper (Ashby / Lever / Greenhouse)
- `GET  /api/dashboard/...` — dashboard tiles
- `POST /api/outreach/generate` — generate outreach message (email / Twitter DM / LinkedIn); thin wrapper over the QA-bank + resume-selection logic
- `GET  /api/outreach/{company_id}` — list previously generated outreach messages for a company, newest first
- `GET  /api/companies` — list saved companies with optional filters (`category`, `source`, `status`, `search`, `limit`, `offset`)
- `GET  /api/companies/stats` — aggregate counts across `status` / `category` / `source`
- `GET  /api/companies/{id}` — single company record (includes `company_summary` + `hiring_signals` for the outreach panel)
- `PATCH /api/companies/{id}/status` — set the lifecycle status (`saved | interested | dismissed | outreach_sent | engaged`); returns the full updated record
- `POST /api/pipeline/run` — kick the LangGraph 4-domain scanner (`funding / remote / ngos / oss`); returns per-domain counts + opportunity payloads
- `GET  /api/pipeline/status` — last-run snapshot (`state`, `last_run_at`, `last_run_duration_seconds`, `last_run_counts`, `recent_error`)
- `GET  /api/pipeline/discover` — kick the boards-only runner (separate from the graph because boards uses a much larger `delta_hours` window)
- `GET  /api/pipeline/schedule` — current `interval_hours` plus the legal-options list + `next_run`
- `PUT  /api/pipeline/schedule` — update `interval_hours`; legal values `{1, 2, 4, 6, 12, 24}` so `422` on anything else
- `GET  /api/pipeline/stats` — dashboard tile counts (`total_companies`, `new_today`, `high_intent`, `contacted`, `ngo_count`) for the React StatusTracker

All `/api/scan/*` endpoints accept `delta_hours`, `limit`, and `sources`
query params so callers can page through by recency. The
`/api/outreach/generate` body shape is
`{company_id, type: "email"|"twitter_dm"|"linkedin", user_context: {name, role, skills: list[str], background}}`; it returns `{id, company_id, type, content, created_at, resume_picked_id, resume_picked_name, qa_snippet_id, qa_snippet}`. Storage is in-memory keyed by `company_id` — messages do not survive process restarts.

`/api/companies` is in-memory seeded with six demo records covering every category and status. Filters compose (`?category=boards&status=interested&search=vercel`); the list response envelope is `{companies: [...], total: matched, count: page_len}` so the React side can render “6 of 47” pagination. `PATCH /api/companies/{id}/status` body is `{status: "saved"|"interested"|"dismissed"|"outreach_sent"|"engaged"}` — these are CRM-style pre-application statuses, distinct from the ApplicationTracker's post-application pipeline (`submitted|interview|rejected|offer|ghosted`).

## Development

> **`.env` reload caveat.** `uvicorn --reload` watches `.py` files, but
> the `load_dotenv` helper in `main.py` runs **only at import time** with
> `override=False`. Edits to `backend/.env` or the repo-root `.env` are
> **not** picked up by a hot reload — the worker process keeps the values
> it loaded on first boot. **Edit `.env` files → bounce the process**
> (`Ctrl-C`, `source .venv/bin/activate && python main.py` again) so the
> new values take effect. See `tests/test_dotenv_loading.py` for the
> proven precedence: shell > `backend/.env` > repo-root `.env`.


```bash
# Unit tests (unittest; pytest is not in pyproject.toml)
python -m unittest discover tests -v

# Single-file drill-down
python -m unittest tests.test_oss -v

# Live smoke against real GitHub (uses real network; budget-aware)
python scripts/oss_smoke.py

# Lint (no backend-side linter is configured yet — add ruff if you want)
```

The OSS live-smoke hits `github.com/trending/<lang>` and the GitHub
Search API in-process; expect ~6 outbound HTTPS calls per run. If you
have a `GITHUB_TOKEN`, both fall under the 5,000/hr authenticated
quota; without one, GitHub may throttle a 3-call burst on trending.

## Where to look

|Find in this file|
|---|
|OSS scraper (trending + GFI + strategy)|`pipeline/nodes/oss/`|
|Other domain runners (funding / ngos / remote / boards)|`pipeline/nodes/{funding,ngos,remote,jobs_boards}/`|
|Graph assembly + per-domain nodes|`pipeline/graph.py`|
|FastAPI routes|`routes/scanner.py`, `routes/dashboard.py`|
|Universal opportunity model|`pipeline/nodes/merge.py`|
|Time-parsing helper (`parse_opportunity_published`)|`utils/time_check.py`|
