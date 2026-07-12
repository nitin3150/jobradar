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
  summary. Used only when ``config/profile.yml`` (and the
  example-file fallback) is empty AND no ``--target-roles`` CLI
  override was passed. Post-merge cleanup dropped the hardcoded
  4-role default; the LLM prompt now renders
  ``"(no profile configured)"`` and the 7-factor SYSTEM_PROMPT
  degrades gracefully when this env var is also unset.
* ``JOB_FIT_THRESHOLD`` — minimum AI fit score (default 0.6).
* ``NVIDIA_BASE_URL`` / ``NVIDIA_MODEL`` / ``GROQ_BASE_URL`` /
  ``GROQ_MODEL`` — LLM endpoint overrides (read by
  :class:`services.llm_client.LLMClient`).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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
from services import profile_service
from services.llm_client import LLMClient
from services.profile_service import (
    Profile,
    TargetRoles,
    get_all_target_roles,
)
from supabase import Client, create_client

# Post-merge cleanup: the hardcoded 4-role DEFAULT_TARGET_ROLES
# fallback is gone. The 3-tier resolution (CLI override → profile.yml
# → TARGET_ROLES env) is the final word — if the operator's profile
# is empty AND the env var is unset, the LLM scoring prompt renders
# ``"(no profile configured)"`` and the 7-factor SYSTEM_PROMPT
# degrades gracefully (no role-fit / seniority-alignment scoring,
# but the other 5 factors still produce a sensible score).
# The previous ``DEFAULT_TARGET_ROLES`` constant mirrored
# ``routes.settings.Preferences.model_fields["target_roles"].default_factory()``
# — a hardcoded list that was the source of truth before
# ``services.profile_service`` existed. With the profile as the
# source of truth, mirroring a hardcoded list defeats the point.


def log(msg: str) -> None:
    """Single GHA-friendly log line prefix. ``flush=True`` so the run log
    shows progress incrementally rather than buffering until job end."""
    print(f"[boards-scan] {msg}", flush=True)


def _resolve_profile(
    cli_target_roles: list[str] | None,
) -> str:
    """Resolve the LLM profile prompt for this scan.

    Resolution order (highest priority first):
    1. ``--target-roles`` CLI flag — explicit one-off override.
       When set, it REPLACES the profile's target_roles (primary
       and archetypes) entirely; the rest of the profile
       (narrative, compensation, location) still flows through.
    2. ``config/profile.yml`` (operator's own) — the primary
       source. Falls back to ``config/profile.example.yml`` when
       the operator hasn't created their own yet.
    3. ``TARGET_ROLES`` env var — comma-separated fallback for
       environments without a profile.yml (e.g. legacy cron
       scripts, one-off test runs).

    Returns the rendered profile markdown block from
    :func:`services.profile_service.build_profile_summary`. An
    empty profile renders as ``"(no profile configured)"`` and the
    LLM scoring degrades gracefully (the SYSTEM_PROMPT's calibration
    clause still produces a reasonable score).

    Note: the script DELIBERATELY mutates the loaded profile's
    ``target_roles`` when applying overrides. The module-level
    cache in :mod:`services.profile_service` is invalidated by
    every ``load_profile`` call (``use_cache=True`` is the default
    but the cache holds the unmutated reference, and we mutate a
    fresh dataclass via ``Profile(**profile.model_copy())`` below),
    so the next boards_scan.py invocation starts clean.
    """
    # Start with the on-disk profile (operator's own, or the
    # example fallback). ``load_profile()`` is cached, so a
    # second call in the same process is free.
    profile = profile_service.load_profile()

    if cli_target_roles:
        # CLI override wins — but ONLY when the override has at
        # least one role. An empty list (``--target-roles=""``)
        # is treated as "operator didn't actually override" so
        # we don't accidentally zero out the profile and feed
        # the LLM ``(no profile configured)``. This matches the
        # intuition that an empty CLI flag is a no-op, not a
        # destructive override.
        profile = profile.model_copy(deep=True)
        profile.target_roles = TargetRoles(primary=list(cli_target_roles))
        log(
            f"using --target-roles override ({len(cli_target_roles)} role(s))"
        )
    elif not get_all_target_roles(profile):
        # Profile is empty (operator cleared their YAML or only
        # the example file is present, and the example has
        # roles — so this branch only fires when the operator
        # explicitly cleared their profile). Fall back to
        # TARGET_ROLES env var, then the Preferences default.
        env_roles = [
            r.strip()
            for r in os.environ.get("TARGET_ROLES", "").split(",")
            if r.strip()
        ]
        if env_roles:
            profile = profile.model_copy(deep=True)
            profile.target_roles = TargetRoles(primary=env_roles)
            log(
                f"profile.yml empty — using TARGET_ROLES env var "
                f"({len(env_roles)} role(s))"
            )
        else:
            # Profile is empty AND the env var is unset. The LLM
            # prompt will render ``"(no profile configured)"`` and
            # the 7-factor SYSTEM_PROMPT degrades gracefully. We
            # previously had a 4th-tier hardcoded fallback here
            # (``DEFAULT_TARGET_ROLES``) — removed in post-merge
            # cleanup so the profile is the only source of truth
            # for target roles.
            log(
                "profile.yml + TARGET_ROLES both empty — LLM will "
                "render '(no profile configured)' and score with the "
                "7-factor SYSTEM_PROMPT's graceful-degradation path"
            )
    else:
        log(
            f"loaded profile from {profile_service.PROFILE_PATH} "
            f"({len(get_all_target_roles(profile))} target role(s))"
        )

    return profile_service.build_profile_summary(profile)


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
        # Default ``None`` (not the env-var value) so the
        # resolution function can tell "operator passed
        # --target-roles='x,y,z'" from "operator didn't pass the
        # flag at all". ``os.environ.get("TARGET_ROLES")`` is
        # still consulted inside :func:`_resolve_profile` as the
        # fallback when profile.yml is empty.
        default=None,
        help=(
            "Comma-separated target roles for a ONE-OFF scan "
            "override. Replaces the profile.yml target_roles "
            "entirely (narrative, compensation, location still "
            "flow through). Default: None — read from profile.yml "
            "(via services.profile_service.load_profile()), "
            "falling back to TARGET_ROLES env var, then the "
            "Preferences default factory."
        ),
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
    ``applied`` / ``flagged`` decision. Returns the number of rows
    that were actually inserted (the rest were duplicates the REST
    API skipped).

    Single-threshold rule: every row that survives the threshold
    filter above is written directly with ``status='approved'``. The
    apply worker (or the operator) picks ``approved`` jobs up on the
    next polling tick. There is no ``in_review`` intermediate — we
    trust the LLM at threshold, so jobs that clear the cutoff are
    eligible for the apply queue immediately. Below-threshold jobs
    are filtered out by :func:`_score_all` / the ``winners`` list
    comprehension in :func:`main` and are NOT written here.
    """
    inserted = 0
    for job, score, reasoning in winners:
        url = job.get("url") or "(no url)"
        # ``posted_at`` / ``source_updated_at`` are populated on
        # the opp dict by ``run_all`` (look for
        # ``job["posted_at"] = parsed`` near the parse_published_at
        # call). They arrive as :class:`datetime.datetime` objects;
        # the Supabase REST layer accepts ISO 8601 strings, so
        # stringify here. ``None`` is fine — Supabase stores NULL
        # and the route's OR-NULL ``posted_at IS NULL`` clause
        # keeps the row in the result set even when the operator
        # sets a date filter (the operator's "date filter is not
        # working" complaint was caused by EVERY row being NULL
        # posted_at under the old code path; with this rendered
        # datetime, dated rows surface correctly).
        posted_at = job.get("posted_at")
        posted_at_iso = (
            posted_at.isoformat() if hasattr(posted_at, "isoformat") else posted_at
        )
        source_updated_at = job.get("source_updated_at")
        source_updated_at_iso = (
            source_updated_at.isoformat()
            if hasattr(source_updated_at, "isoformat")
            else source_updated_at
        )
        # ``job["ats_type"]`` is set in ``run_all`` to the SPECIFIC
        # board ("ashby" | "greenhouse" | "lever") — the operator
        # reported "Board name is saying Board it should say the
        # Boards name like lever, ashby or greenhouse" because the
        # old hardcoded "boards" string title-cased to "Board".
        # Fall back to "boards" only if the runner didn't inject
        # (defensive — should never fire with the updated runner).
        ats_type = job.get("ats_type") or "boards"
        row = {
            "id": _job_id(url),
            # Single-threshold rule: above ``--threshold`` ⇒
            # ``approved`` (auto-apply queue), below ⇒ not persisted.
            # See module docstring for the rationale.
            "status": "approved",
            "ats_type": ats_type,
            "title": (job.get("title") or "(untitled)")[:500],
            # ``company_name`` is set by ``run_all`` from
            # ``_display_name_for_slug(slug)`` when the fetcher
            # didn't surface one. The fallback "(unknown)" only
            # fires for jobs written before the runner fix.
            "company_name": (job.get("company_name") or "(unknown)")[:500],
            "url": url[:1000],
            "ai_fit_score": round(max(0.0, min(1.0, score)), 4),
            "ai_fit_reasoning": (reasoning or "")[:1000],
            # ``description`` is fetched by ``run_all`` (Greenhouse's
            # ``content``, Lever's ``descriptionPlain``/``description``,
            # Ashby's ``descriptionPlain``/``description`` — see
            # ``pipeline/nodes/jobs_boards/{greenhouse,lever,ashby}.py``)
            # and is preserved in the ``job`` dict through scoring + this
            # persist call. The column exists on ``jobs`` (TEXT, nullable)
            # from migration ``0003_add_job_description``. The previous
            # version of this script omitted ``description`` from the row
            # dict — every DB row persisted by the GHA cron therefore had
            # ``description IS NULL`` and any post-hoc regex / full-text
            # query against the queue was useless. Mirroring the
            # SQLAlchemy persist path's behaviour in
            # ``services.scoring_service._opportunity_to_job_fields``:
            # ``or None`` so empty/missing fetches land as SQL NULL
            # rather than an empty-string sentinel, which keeps the
            # schema consistent across both persist paths.
            "description": job.get("description") or None,
            "posted_at": posted_at_iso,
            "source_updated_at": source_updated_at_iso,
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


# ----------------------------------------------------------------------
# Boards-scan audit logging
# ----------------------------------------------------------------------
# Every invocation of ``main()`` opens a ``scanner_runs`` row at
# start and closes it in a ``finally`` block so the postmortem
# timeline can be reconstructed from SQL alone — not from the GHA run
# log (which is per-run_id and ephemeral). The three new columns
# (``tier``, ``env_hash``, ``jobs_persisted``) were added in
# migration 0005 specifically so this module has somewhere to write
# its per-invocation audit rope. See that migration's docstring for
# the schema rationale; the helpers here implement the writer side.
#
# Failure-mode design (best-effort, NOT strict):
#   * If :func:`record_audit_open` fails (Supabase 401, network blip,
#     table doesn't exist), we log a WARN and continue. The scan
#     still lands its jobs in the ``jobs`` table; only the audit
#     row is missing. A postmortem that asks "did the cron fire?"
#     for that one tick returns "no audit row exists" rather than
#     "the script crashed" — a low-stakes answer.
#   * If :func:`record_audit_close` fails, we log a WARN and the
#     row stays at ``state='running'``. A separate cleanup cron
#     can sweep stuck-running rows (the operator can run
#     ``UPDATE scanner_runs SET state='error' WHERE state='running'
#     AND started_at < NOW() - INTERVAL '1 hour'`` in the Supabase
#     Studio to do this manually today).
#
# This is deliberately best-effort because the production data path
# (the ``jobs`` table) is more valuable than the audit trail —
# losing one audit row is recoverable from GHA logs; losing the
# daily scraped-job inserts is not.
# ----------------------------------------------------------------------


def _compute_env_hash() -> str:
    """sha256-hex digest of the env vars that govern this run's behavior.

    Same env → same hash; any change to a contributing var produces
    a different hash so a postmortem can ``GROUP BY env_hash`` over
    the last 30 days and instantly see "which config change first
    produced zero writes".

    Excluded on purpose:
    * Secret-carrying env vars (``*_API_KEY``, ``*_TOKEN``,
      ``SUPABASE_SERVICE_ROLE_KEY``, etc.) — even a hash of a
      secret is a privilege-escalation risk if the GH repo or this
      DB is later compromised (a known chain is enough to narrow the
      brute-force space).
    * Drift-prone vars (``PATH``, ``HOME``, ``USER``, ``LANG``,
      ``SHELL``, ``PWD``) — same operator + same binary + different
      shell environment would otherwise produce different hashes
      for no observable behavior change.
    * Variables set by the Python interpreter (``VIRTUAL_ENV``,
      ``PYTHONPATH``, ``OLDPWD``) — same reasoning.

    >>> os.environ.update({"BOARDS_DELTA_HOURS": "24"})
    >>> h1 = _compute_env_hash()
    >>> os.environ["JOB_FIT_THRESHOLD"] = "0.7"
    >>> _compute_env_hash() == h1  # var outside our list ⇒ unchanged
    True
    """
    keys = [
        "BOARDS_DELTA_HOURS",
        "BOARDS_LIMIT",
        "BOARDS_BOARDS",
        "BOARDS_HTTP_TIMEOUT",
        "BOARDS_SKIP_TIMEOUTS",
        "BOARDS_PROFILE_PATH",
        "JOB_FIT_THRESHOLD",
        "TARGET_ROLES",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "GITHUB_SHA",
        "GITHUB_REF",
    ]
    # ``strip()`` so a whitespace-only value is treated as "not set"
    # — the operator might export an empty value during a misconfig
    # experiment and we don't want that to perturb the hash.
    parts = [
        f"{k}={os.environ.get(k, '').strip()}"
        for k in keys
        if os.environ.get(k, "").strip()
    ]
    # Sort for determinism — same env content in any `os.environ`
    # iteration order produces the same hash.
    canonical = "\n".join(sorted(parts))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_audit_open(sb: Client) -> str | None:
    """Insert a ``scanner_runs`` row at script start.

    Returns the row's UUID string on success, or ``None`` if the
    INSERT failed (in which case :func:`record_audit_close` will
    no-op rather than try to update a non-existent row). Logs WARN
    on failure but does NOT raise — losing the audit row is
    recoverable from GHA run logs; raising here would lose the
    scan's ``jobs`` writes alongside the audit row.
    """
    env_hash = _compute_env_hash()
    tier = os.environ.get("BOARDS_TIER") or "manual"
    audit_id = str(uuid4())

    # Render the start timestamp with a trailing ``Z`` rather than
    # ``+00:00`` so a JSONB-shaped ``scanner_runs`` row matches the
    # same wire format the FastAPI ``_iso_utc`` helper already uses
    # for ``jobs.posted_at``. Self-consistency matters more here
    # than absolute ISO strictness — both are valid ISO 8601.
    started_at_iso = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    row = {
        "id": audit_id,
        "scanner": "boards",
        "tier": tier,
        "state": "running",
        "started_at": started_at_iso,
        "env_hash": env_hash,
        "items_found": 0,
        "error_count": 0,
        "error_summary": None,
        "jobs_persisted": 0,
    }
    try:
        sb.table("scanner_runs").insert(row).execute()
        # Truncate the env_hash log line — the full hex is
        # retrievable from the DB; the first 8 chars are enough for
        # the operator to confirm "is this the same env as the
        # previous run?" while reading the GHA log.
        log(
            f"audit open: id={audit_id} tier={tier} "
            f"env_hash={env_hash[:8]}…"
        )
        return audit_id
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        log(
            f"WARN audit_open failed: {exc} "
            f"(scan continues without the audit row)"
        )
        return None


def record_audit_close(
    sb: Client,
    audit_id: str | None,
    *,
    items_found: int,
    jobs_persisted: int,
    state: str,
    error_summary: str | None,
) -> None:
    """Update the open ``scanner_runs`` row with closing state.

    No-op when :func:`record_audit_open` returned ``None`` (the
    open failed; there's no row to close). Best-effort on the
    UPDATE itself — NEVER raises because the close always runs from
    a ``finally`` block and a raised exception here would mask the
    actual script's success/failure for the operator reading the
    GHA workflow log.
    """
    if audit_id is None:
        return  # no row opened; nothing to close

    # The schema column ``error_summary`` is sized INTENTIONALLY
    # small (1 KB) so a single wild exception can't blow the row
    # out of the Postgres TOAST chunk; truncate defensively rather
    # than rely on the DB to reject. Strip empty-string None.
    finished_at_iso = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    finish = {
        "state": state,
        "finished_at": finished_at_iso,
        "items_found": items_found,
        "jobs_persisted": jobs_persisted,
        "error_count": 1 if error_summary else 0,
        "error_summary": (
            (error_summary or "")[:1000] or None
        ),
    }
    try:
        sb.table("scanner_runs").update(finish).eq(
            "id", audit_id
        ).execute()
        log(
            f"audit close: id={audit_id} state={state} "
            f"items_found={items_found} jobs_persisted={jobs_persisted} "
            f"errors={'yes' if error_summary else 'no'}"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        log(
            f"WARN audit_close failed: {exc} "
            f"(row may be stuck at state=running; "
            f"sweep with: UPDATE scanner_runs SET state='error' "
            f"WHERE state='running' AND started_at < NOW() - INTERVAL '1 hour')"
        )


# ----------------------------------------------------------------------
# main()
# ----------------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    started_wall = time.monotonic()

    # ---- 0. Supabase auth — required for BOTH audit + persist ----------
    # Required up-front rather than just before persist so the
    # audit-row open call uses the same client (one HTTP connection
    # to PostgREST, one TLS handshake). The historical exit code 2
    # ("Supabase env missing") is preserved — meaning unchanged
    # even though the location moved.
    # A misconfigured GHA cron that bails here costs <1s vs the old
    # 100s+ of setup-time + boards-runner scraping before the
    # persist path discovered the misconfig. The boards-scan's
    # ~$0.20 of LLM cost is also preserved by the fail-fast.
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        log("ERROR SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are required")
        return 2

    sb = create_client(supabase_url, supabase_key)
    audit_id = record_audit_open(sb)  # best-effort; ``None`` on failure

    # Local state captured across the try/finally — defaults are
    # ``state='error'`` so a KeyboardInterrupt or unhandled
    # BaseException that bypasses the ``except Exception`` branches
    # STILL records ``state='error'`` rather than the misleading
    # ``state='idle'``. The five success-path ``return`` statements
    # each explicitly overwrite ``final_state = 'idle'`` before
    # returning; the two error-path ``return`` statements keep
    # ``final_state = 'error'`` and write ``error_summary``.
    items_found = 0
    jobs_persisted = 0
    final_state = "error"
    error_summary = None

    try:
        # ---- 1. Run the boards runner ------------------------------------
        log(
            f"starting: delta_hours={args.delta_hours} "
            f"boards={args.boards} limit={args.limit or 'none'} "
            f"threshold={args.threshold} dry_run={args.dry_run}"
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
            error_summary = f"boards runner: {type(exc).__name__}: {exc}"
            return 1

        items_found = len(jobs)
        log(
            f"runner returned {items_found} relevant jobs in "
            f"{time.monotonic() - started_wall:.1f}s"
        )

        if not jobs:
            log("no relevant jobs to score — exiting cleanly")
            final_state = "idle"  # ran end-to-end, found nothing
            return 0

        # ---- 2. Build the profile + spin up the LLM client --------------
        cli_target_roles = (
            [r.strip() for r in args.target_roles.split(",") if r.strip()]
            if args.target_roles is not None
            else None
        )
        profile = _resolve_profile(cli_target_roles)

        try:
            llm = LLMClient.from_env()
        except RuntimeError as exc:
            log(f"ERROR LLM client init: {exc}")
            error_summary = f"LLM init: {exc}"
            return 1

        # ---- 3. Score every job, filter by threshold --------------------
        log(f"scoring {items_found} jobs (threshold >= {args.threshold})")
        try:
            scored = asyncio.run(_score_all(llm, profile, jobs))
        except Exception as exc:
            log(f"ERROR scoring crashed: {exc}")
            traceback.print_exc()
            error_summary = f"scoring: {type(exc).__name__}: {exc}"
            return 1

        winners = [(j, s, r) for j, s, r in scored if s >= args.threshold]
        log(
            f"winners: {len(winners)}/{items_found} above threshold "
            f"(mean score {sum(s for _, s, _ in winners) / max(1, len(winners)):.3f})"
        )

        if not winners or args.dry_run:
            if args.dry_run:
                log("dry-run: skipping Supabase insert")
            final_state = "idle"
            return 0

        # ---- 4. Persist winners via the Supabase REST API --------------
        inserted = _persist_winners(sb, winners)
        jobs_persisted = inserted
        log(
            f"persisted {inserted}/{len(winners)} winners to Supabase "
            f"(total wall-clock: {time.monotonic() - started_wall:.1f}s)"
        )
        final_state = "idle"
        return 0
    finally:
        # ALWAYS close the audit row — even on KeyboardInterrupt /
        # SIGTERM — so a postmortem query never sees
        # ``state='running'`` for a row whose process has died. The
        # close itself is best-effort (warns on failure, never
        # raises) so an exception here cannot mask the script's
        # actual success/failure code that the operator sees in the
        # GHA workflow log.
        record_audit_close(
            sb,
            audit_id,
            items_found=items_found,
            jobs_persisted=jobs_persisted,
            state=final_state,
            error_summary=error_summary,
        )


if __name__ == "__main__":
    sys.exit(main())
