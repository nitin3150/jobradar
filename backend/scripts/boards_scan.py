"""Boards scan wrapper for GitHub Actions (and local CLI use).

The boards runner at ``pipeline.nodes.jobs_boards.runner.run_all`` is
designed to be called from a long-running FastAPI process; the runner
returns the filtered job list and the FastAPI route handler in
``routes.scanner`` does the LLM scoring + Supabase persistence. This
script folds those two steps into a single CLI so the GitHub Actions
workflow can invoke the runner directly (no backend deploy required).

What this script does
====================

1. Calls ``run_all(delta_hours, boards, limit)`` and gets back the
   list of role-relevant jobs that pass the heuristic filter.
2. Builds a profile summary from the ``TARGET_ROLES`` env var
   (defaults to the same target roles the Preferences singleton ships
   with — same wire shape as the React ``PreferencesModal``).
3. Scores each job via :class:`services.llm_client.LLMClient`
   (NVIDIA-primary, Groq-fallback — picks the provider that has a
   key in the env).
4. Filters by ``--threshold`` (default 0.6) and upserts the winners
   into the ``jobs`` table on Supabase via the supabase-py client
   (``POST /rest/v1/jobs`` with ``Prefer: resolution=ignore-duplicates``).

Why supabase-py (REST) instead of SQLAlchemy (asyncpg)
======================================================

The existing ``services.scoring_service.score_and_persist`` writes
via SQLAlchemy + asyncpg, which requires a direct
``DATABASE_URL`` connection from the GHA worker. Using the Supabase
REST API instead means the GHA job only needs ``SUPABASE_URL`` +
``SUPABASE_SERVICE_ROLE_KEY`` — no Postgres connection pool, no
pgBouncer configuration, no asyncpg in the worker image. The
trade-off is that the REST API doesn't support ``ON CONFLICT (id)
DO UPDATE`` semantics directly; we use
``ignore_duplicates=True`` so re-running the scan is idempotent and
NEVER resets an operator's approval/rejection of an existing row.

``seen.json`` / ``last_run.json`` caveat
=======================================

The boards runner writes per-org dedupe state to
``backend/data/seen.json`` etc. on every run. In GHA that file is
ephemeral — the next cron tick starts with an empty ``seen.json``
and re-processes every recent job. The ``--limit`` arg caps the
per-run org count to keep the wall-clock + LLM cost bounded
(defaults to 200 so an hourly cron lands inside the 2,000 GHA
minutes/month free tier). For persistent cross-run dedupe, lift the
runner onto a host with persistent disk (Oracle Cloud Always Free
ARM, Render paid, etc.) or extend the runner to use Supabase as
its seen-store.

Required env vars
=================

* ``SUPABASE_URL`` — ``https://<project-ref>.supabase.co``
* ``SUPABASE_SERVICE_ROLE_KEY`` — service_role secret (server-only)
* ``GROQ_API_KEY`` and/or ``NVIDIA_API_KEY`` — LLM provider keys

Optional env vars
=================

* ``TARGET_ROLES`` — comma-separated target roles for the profile
  summary. Defaults to the same four the Preferences singleton ships.
* ``JOB_FIT_THRESHOLD`` — minimum AI fit score (default 0.6).
* ``NVIDIA_BASE_URL`` / ``NVIDIA_MODEL`` / ``GROQ_BASE_URL`` /
  ``GROQ_MODEL`` — LLM endpoint overrides (read by
  :class:`services.llm_client.LLMClient`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

# Make ``backend/`` importable so ``from pipeline...`` etc. resolves
# regardless of the working directory GHA invokes the script with.
# This ``sys.path.insert`` is technically redundant when the
# workflow runs ``pip install -e .`` (editable install registers
# the package on the import path), but it's defensive — running
# the script as ``python scripts/boards_scan.py`` from a fresh
# checkout without the install step would otherwise fail at
# import time. The boards runner itself does the same thing.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pipeline.nodes.jobs_boards.runner import run_all
from services.llm_client import LLMClient
from supabase import Client, create_client

# Import the Preferences Pydantic model so the script's default
# target roles are read from the SINGLE source of truth — drift
# between the GHA worker's profile and the dev path is structurally
# impossible. ``default_factory()`` invokes the lambda the model
# declares, so editing the list in :class:`routes.settings.Preferences`
# flows here without a second hand-edited mirror.
from routes.settings import Preferences as _Preferences

DEFAULT_TARGET_ROLES: list[str] = list(
    _Preferences.model_fields["target_roles"].default_factory()
)


def log(msg: str) -> None:
    """Single GHA-friendly log line prefix. ``flush=True`` so the run log
    shows progress incrementally rather than buffering until job end."""
    print(f"[boards-scan] {msg}", flush=True)


def _profile_summary(target_roles: list[str]) -> str:
    """Build the same ``profile_summary`` shape that the
    ``scoring_service.build_profile_summary`` helper produces in the
    FastAPI path. We construct a minimal version here because the
    GHA worker has no populated ``_PREFS_STATE`` (no operator PATCH
    ever ran) and no ``_QA_DB`` (the Q&A bank isn't bootstrapped).
    """
    if not target_roles:
        return "(no profile configured)"
    return "Target roles:\n" + "\n".join(f"- {r}" for r in target_roles)


def _job_id(url: str) -> str:
    """Stable UUID5-derived id matching the formula in
    :func:`services.scoring_service._job_id` so a re-run with the
    same URL hits the same PK and ``ignore_duplicates=True`` skips
    the insert instead of duplicating."""
    return str(uuid5(NAMESPACE_URL, f"boards:{url}"))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--delta-hours",
        type=int,
        default=int(os.environ.get("BOARDS_DELTA_HOURS", "1")),
        help="Lookback window in hours. Default: 1 (active tier).",
    )
    p.add_argument(
        "--boards",
        nargs="*",
        default=os.environ.get("BOARDS_BOARDS", "ashby greenhouse lever").split(),
        help="Boards to scrape. Default: ashby greenhouse lever.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("BOARDS_LIMIT", "200")),
        help="Per-board org cap. Default 200 keeps the hourly cron "
        "inside the 2,000 GHA minutes/month free tier. Pass 0 for "
        "no cap (deploys with persistent disk only).",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("JOB_FIT_THRESHOLD", "0.6")),
        help="Minimum AI fit score (0.0-1.0) for a job to land in "
        "the ``jobs`` table. Default 0.6.",
    )
    p.add_argument(
        "--target-roles",
        type=str,
        default=os.environ.get("TARGET_ROLES", ",".join(DEFAULT_TARGET_ROLES)),
        help="Comma-separated target roles for the profile summary. "
        "Default: the four the Preferences singleton ships with.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the boards runner + LLM scoring but skip the "
        "Supabase insert. Useful for cron-day validation before "
        "committing real Groq/NVIDIA spend.",
    )
    return p.parse_args()


async def _score_all(
    client: LLMClient,
    profile: str,
    jobs: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], float, str]]:
    """Score every job with a bounded concurrency of 8 (matches the
    runner's ``MAX_WORKERS`` so we don't oversubscribe the LLM rate
    limit on a free tier). Returns ``(job, score, reasoning)`` for
    each job that cleared the threshold.
    """
    sem = asyncio.Semaphore(8)

    async def _one(job: dict[str, Any]) -> tuple[dict[str, Any], float, str] | None:
        async with sem:
            try:
                score, reasoning = await client.score_opportunity(profile, job)
            except RuntimeError as exc:
                log(f"WARN scoring {job.get('url')}: {exc}")
                return None
            return job, score, reasoning

    results = await asyncio.gather(*[_one(j) for j in jobs])
    return [r for r in results if r is not None]


def _persist_winners(
    sb: Client,
    winners: list[tuple[dict[str, Any], float, str]],
) -> int:
    """Upsert each winner into the ``jobs`` table via the Supabase
    REST API with ``ignore_duplicates=True`` so re-running the cron
    is idempotent and never overwrites an operator's later
    ``approved`` / ``rejected`` decision. Returns the number of
    rows that were actually inserted (the rest were duplicates the
    REST API skipped).
    """
    inserted = 0
    for job, score, reasoning in winners:
        url = job.get("url") or "(no url)"
        row = {
            "id": _job_id(url),
            "status": "in_review",
            "ats_type": "boards",
            "title": (job.get("title") or "(untitled)")[:500],
            "company_name": (job.get("company_name") or "(unknown)")[:500],
            "url": url[:1000],
            "ai_fit_score": round(max(0.0, min(1.0, score)), 4),
            "ai_fit_reasoning": (reasoning or "")[:1000],
        }
        try:
            # ``ignore_duplicates=True`` is the supabase-py spelling
            # of ``ON CONFLICT (id) DO NOTHING``. Without it, every
            # cron tick would overwrite the operator's prior
            # ``status`` column.
            sb.table("jobs").upsert(
                row,
                on_conflict="id",
                ignore_duplicates=True,
            ).execute()
            inserted += 1
        except Exception as exc:  # noqa: BLE001 — log + continue
            log(f"WARN persist {url}: {exc}")
    return inserted


def main() -> int:
    args = _parse_args()
    started = time.monotonic()

    # ---- 1. Run the boards runner ----------------------------------------
    log(
        f"starting: delta_hours={args.delta_hours} boards={args.boards} "
        f"limit={args.limit or 'none'} threshold={args.threshold} "
        f"dry_run={args.dry_run}"
    )
    try:
        jobs = run_all(
            delta_hours=args.delta_hours,
            boards=args.boards,
            limit=args.limit if args.limit > 0 else None,
        )
    except Exception as exc:
        log(f"ERROR boards runner crashed: {exc}")
        traceback.print_exc()
        return 1
    log(f"runner returned {len(jobs)} relevant jobs in {time.monotonic() - started:.1f}s")

    if not jobs:
        log("no relevant jobs to score — exiting cleanly")
        return 0

    # ---- 2. Build the profile + spin up the LLM client ------------------
    target_roles = [r.strip() for r in args.target_roles.split(",") if r.strip()]
    profile = _profile_summary(target_roles)
    log(f"scoring with profile: {len(target_roles)} target role(s)")

    try:
        llm = LLMClient.from_env()
    except RuntimeError as exc:
        log(f"ERROR LLM client init: {exc}")
        return 1

    # ---- 3. Score every job, filter by threshold ------------------------
    log(f"scoring {len(jobs)} jobs (threshold >= {args.threshold})")
    try:
        scored = asyncio.run(_score_all(llm, profile, jobs))
    except Exception as exc:
        log(f"ERROR scoring crashed: {exc}")
        traceback.print_exc()
        return 1
    winners = [(j, s, r) for j, s, r in scored if s >= args.threshold]
    log(
        f"winners: {len(winners)}/{len(jobs)} above threshold "
        f"(mean score {sum(s for _, s, _ in winners) / max(1, len(winners)):.3f})"
    )

    if not winners or args.dry_run:
        if args.dry_run:
            log("dry-run: skipping Supabase insert")
        return 0

    # ---- 4. Persist winners via the Supabase REST API -------------------
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        log("ERROR SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are required for persist")
        return 2

    sb = create_client(supabase_url, supabase_key)
    inserted = _persist_winners(sb, winners)
    log(
        f"persisted {inserted}/{len(winners)} winners to Supabase "
        f"(total wall-clock: {time.monotonic() - started:.1f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
