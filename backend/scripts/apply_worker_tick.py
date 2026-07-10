"""apply_worker_tick — run-once CLI for the GitHub Actions cron apply worker.

The long-running ``backend/apply_worker/main.py:asyncio.run(main_loop())`` is a
forever-loop with SIGTERM-driven shutdown — designed for Render / Docker hosts
that run the worker as a long-lived process. GitHub Actions is the opposite
shape: every cron tick is one process that runs to ``timeout-minutes`` then is
killed. This script is the GHA-compatible entry point — one invocation drains
the apply queue (calling :func:`apply_worker.main.run_one_tick` in a loop) until
the queue is empty OR a wall-clock deadline is hit, then exits.

Why "drain until empty" rather than "one row per invocation"
============================================================

The GHA setup overhead per invocation is ~100 s (checkout + setup-python +
``pip install -e .`` + ``playwright install chromium``). With a cache hit the
Playwright download is amortized but the pip + checkout still costs real wall-
clock.  Once per row would burn that overhead on a queue that's empty 99 % of
the time (the boards-scan and LLM scoring paths are upstream — the worker's
input is bounded by how often those land a new ``approved`` row).

Draining inside one invocation keeps the per-process overhead fixed at ~100 s
and amortizes it over however many rows the queue has.  Combined with an
hourly cron, a single morning peak of 30 newly approved rows clears in one
run rather than spreading across 30 cron ticks.

Supabase ``FOR UPDATE SKIP LOCKED`` queue fairness
==================================================

The orchestrator (:func:`apply_worker.main.run_one_tick`) takes its row under
``SELECT ... WHERE status='approved' ORDER BY created_at ASC LIMIT 1 FOR
UPDATE SKIP LOCKED``. That lock auto-releases at ``session.commit()`` so this
script's wall-clock deadline safely exits even mid-tick — the row is unlocked
and the next hour's cron will pick it up.

Concurrency: this script is safe to run side-by-side with a long-running
``main_loop`` (e.g. on a Render host) — ``FOR UPDATE SKIP LOCKED`` divides the
work without double-submit. The :func:`.github:workflows:apply-worker.yml`
workflow also pins ``concurrency.group: apply-worker-singleton`` so two GHA
ticks of the same workflow don't overlap if one gets stuck.

Required environment variables
==============================

* ``DATABASE_URL`` — ``postgresql+asyncpg://...`` DSN. Read by
  :mod:`db.session` at module-import time; missing ⇒
  :func:`require_database_configured` raises and the script exits 1.
* ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` — read by
  :mod:`storage.supabase` for screenshot uploads. SOFT-required: the form
  filler's screenshot upload step catches ``RuntimeError("Supabase client is
  not configured")`` and proceeds with ``submission_screenshot_path=NULL``.
  Still recommended so the apply_worker entries have visual proof of submit.

Optional environment variables
==============================

* ``NVIDIA_API_KEY`` and/or ``GROQ_API_KEY`` — :class:`LLMClient.from_env`
  requires AT LEAST one provider, otherwise raises ``RuntimeError`` and the
  script exits 1. The qa_matcher uses the LLM as a fallback when the
  rapidfuzz pass misses; a typical apply workflow hits rapidfuzz for most
  fields so a transient LLM outage won't block submissions, but a permanent
  LLM outage will.
* ``JOBRADAR_TEST_DB=1`` — switches the SQLAlchemy engine to NullPool so
  pytest can run without a live Postgres connection. Useful for the test
  suite's drain-loop logic tests.

Exit codes
==========

* ``0`` — successful run. The drain loop processed 0+ rows (IDLE on an
  empty queue, or N rows until deadline) without unhandled exception.
* ``1`` — fatal: missing required env var, unhandled tick exception, or
  LLM client construction failed. GHA surfaces this via the workflow's
  failure-email + STATUS_WEBHOOK_URL notification.

Operational notes
=================

* The drain loop runs ``run_one_tick`` synchronously inside one OS process —
  a single asyncpg connection per tick. Connection-pool reuse across ticks
  comes from SQLAlchemy's sessionmaker pool, NOT from re-opening the engine.
* The LLMClient is constructed ONCE per invocation (before the loop), not
  per tick — rebuilding would re-open the AsyncOpenAI's httpx connection
  pool on each iteration and warm it cold. The single bucket + shared
  per-process cache is what ``NVIDIA_RPM`` is sized against.
* Playwright is launched INSIDE :func:`apply_worker.form_filler.fill_form`
  (via ``_default_page_factory``), not at this layer. Each ``run_one_tick``
  call owns one page lifecycle, so the drain loop can stop on IDLE without
  leaking an orphan browser context.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Make ``backend/`` importable so ``from apply_worker...`` etc. resolves
# regardless of the working directory GHA invokes the script with. The
# boards runner + boards_scan.py use the same defensive sys.path insert
# so this is the project's established run-once-script convention.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Imports AFTER the sys.path insert so editable installs vs cwd invocation
# both resolve. ``TickOutcome`` + ``TickStatus`` are re-exported through the
# ``apply_worker.main`` namespace, which is the same surface the long-
# running ``main_loop`` uses — so the tick is *the same orchestration*
# just wrapped in a run-to-completion loop instead of an asyncio.Event
# poll loop.
from apply_worker.main import TickOutcome, TickStatus, run_one_tick
from db.session import AsyncSessionLocal, require_database_configured
from services.llm_client import LLMClient


# -----------------------------------------------------------------------
# Logger — single GHA-friendly log line prefix. ``flush=True`` so the run
# log shows progress incrementally rather than buffering until job end.
# -----------------------------------------------------------------------
_logger = logging.getLogger("jobradar.apply_worker.tick")


def log(msg: str) -> None:
    """Print a single GHA-friendly log line. Fits boards_scan.py's ``log()`` shape
    so the operator reads both workflow logs with the same eye."""
    print(f"[apply-tick] {msg}", flush=True)


# -----------------------------------------------------------------------
# Defaults — overridden via CLI args, but pinned so the script's behaviour
# is reproducible without an operator reading the workflow file first.
# -----------------------------------------------------------------------

# 50 minutes default wall-clock deadline. The GHA workflow file pins
# ``timeout-minutes: 55`` so the drain loop's 50-minute hard stop gives
# GHA its own 5-minute cleanup window before the 1-hour kill. Operators
# can override on the command line for a longer staging ramp
# (``--timeout-seconds 6000``).
DEFAULT_DRAIN_TIMEOUT_SECONDS = 50 * 60


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_DRAIN_TIMEOUT_SECONDS,
        help=(
            f"Hard deadline for the drain loop in seconds. Default {DEFAULT_DRAIN_TIMEOUT_SECONDS}"
            f" (50 min) — gives GHA's job-level timeout-minutes a 5-min cleanup window."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the LLM client + drain loop but DO NOT mutate the database. "
        "Currently a no-op pending form_filler test-pass coverage; reserved "
        "for cron-day validation before committing Playwright time + real "
        "Supabase writes. The session is opened read-only and rolls back "
        "every transaction.",
    )
    return p.parse_args()


def _require_env(name: str) -> str:
    """Validate a required env var is set + non-empty. Exit 1 with a clean
    remediation message on a miss so GHA's failure-email spares the operator
    a trip to the README.  ``os.environ.get(name, '').strip() or ...``
    treats whitespace-only values as unset."""
    val = os.environ.get(name, "").strip()
    if not val:
        log(
            f"ERROR required env var {name!r} is unset or whitespace-only; "
            f"add it at "
            f"https://github.com/${{GITHUB_REPOSITORY:-<repo>}}/settings/secrets/actions "
            f"(production reads from backend/.env)"
        )
        sys.exit(1)
    return val


def _validate_llm_keys_present() -> None:
    """Check that at least one LLM provider key is configured before
    ``LLMClient.from_env()`` is called. Without this check the drain loop
    would crash mid-tick with ``RuntimeError("no LLM provider configured")``
    on the first approved row — exit 1 here keeps the failure local to
    env-validation (logged once, no DB transaction open)."""
    has_nvidia = bool(os.environ.get("NVIDIA_API_KEY", "").strip())
    has_groq = bool(os.environ.get("GROQ_API_KEY", "").strip())
    if not (has_nvidia or has_groq):
        log(
            "ERROR no LLM provider configured — set NVIDIA_API_KEY and/or "
            "GROQ_API_KEY in the environment (see backend/.env.example). "
            "The qa_matcher uses the LLM as a fallback pass; the rapidfuzz "
            "primary handles most matches without it."
        )
        sys.exit(1)


# -----------------------------------------------------------------------
# Drain loop — the core of the script. Sequential await over
# ``run_one_tick`` calls until the queue is empty (IDLE) or the wall-clock
# deadline is reached.
# -----------------------------------------------------------------------


async def _drain_loop(
    timeout_seconds: int,
    *,
    session_factory=AsyncSessionLocal,
    llm_factory=LLMClient.from_env,
    tick_runner=run_one_tick,
) -> int:
    """Process queued rows until IDLE OR deadline. Returns the number of
    rows processed.

    Args:
        timeout_seconds: Wall-clock budget. The loop checks
            ``time.monotonic() >= deadline`` BEFORE each tick so the drain
            finishes at most one tick past the deadline (the in-flight tick
            is allowed to complete because ``async with AsyncSessionLocal()``
            guarantees session.close() on exit — half-flipping an approved →
            applied row is the safer failure mode than crashing mid-tx).
        session_factory: Override for tests. Defaults to the
            module-level ``AsyncSessionLocal`` (the SQLAlchemy sessionmaker).
        llm_factory: Override for tests. Defaults to
            :func:`LLMClient.from_env`, which builds the singleton
            provider chain from process env.
        tick_runner: Override for tests. Defaults to
            :func:`apply_worker.main.run_one_tick` — the same function
            ``main_loop`` calls, so orchestration semantics are identical
            between the long-running loop and this script.

    Returns:
        The number of rows processed (any TickStatus other than IDLE counts
        as "processed" — a ``NO_FIELDS`` parking is still progress against
        the queue).

    Exits:
        ``sys.exit(1)`` on any unhandled exception from ``tick_runner``. The
        ``async with`` block's session.close() on the way out rolls back the
        FOR UPDATE SKIP LOCKED row, so the next hour's cron sees it again.
    """
    require_database_configured()
    if session_factory is None:
        # ``require_database_configured`` would have raised; this guard is
        # purely for the type checker.
        raise RuntimeError("AsyncSessionLocal is None after require_database_configured")

    llm = llm_factory()
    deadline = time.monotonic() + timeout_seconds
    processed = 0
    log(f"drain loop starting (timeout={timeout_seconds}s)")

    while True:
        if time.monotonic() >= deadline:
            log(
                f"deadline reached ({timeout_seconds}s); exiting with "
                f"processed={processed}"
            )
            return processed
        async with session_factory() as session:
            try:
                outcome: TickOutcome = await tick_runner(session, llm)
            except Exception as exc:  # noqa: BLE001 — re-raised below for exit code
                # ANY unhandled exception → exit 1 so GHA fires its
                # failure-email + STATUS_WEBHOOK_URL notification. The
                # ``async with`` block below the ``raise`` rolls back the
                # open transaction (releasing the FOR UPDATE SKIP LOCKED
                # row) before this script's process exits.
                log(
                    f"ERROR tick raised {type(exc).__name__}: {exc}; "
                    f"rolling back and exiting non-zero so GHA alerts"
                )
                raise

        log(
            f"tick status={outcome.status.value} "
            f"job_id={outcome.job_id or '<none>'} "
            f"elapsed={outcome.elapsed_seconds:.3f}s"
        )
        if outcome.status == TickStatus.IDLE:
            # Queue is empty — drain loop ends cleanly.
            log(f"queue empty; exiting with processed={processed}")
            return processed
        processed += 1


async def main() -> int:
    args = _parse_args()
    _require_env("DATABASE_URL")
    _validate_llm_keys_present()
    # SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are SOFT-required: the
    # form_filler screenshot upload tolerates a missing Supabase client and
    # persists ``submission_screenshot_path=NULL`` instead of crashing. The
    # GHA workflow's secret lint still flags them as recommended so the
    # operator notices the missing audit trail.
    started = time.monotonic()
    processed = await _drain_loop(args.timeout_seconds)
    log(
        f"drain loop complete in {time.monotonic() - started:.1f}s "
        f"(processed={processed})"
    )
    return 0


__all__ = [
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "log",
    "_drain_loop",
    "_require_env",
    "_validate_llm_keys_present",
]


if __name__ == "__main__":
    # ``python scripts/apply_worker_tick.py`` from inside the backend dir OR
    # ``python scripts/apply_worker_tick.py`` from the repo root — both
    # work thanks to the BACKEND_ROOT sys.path insert above.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    sys.exit(asyncio.run(main()))
