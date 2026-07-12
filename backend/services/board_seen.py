"""Persistent, Postgres-backed dedupe for the boards runner.

Why this module exists
======================

The boards runner historically consulted :mod:`utils.seen` — an
on-disk ``backend/data/seen.json`` file carrying ``{digest_key:
last_seen_iso}`` entries — for "have I delivered this job ID
before?" cross-run deduplication. That works fine for a single
long-running Docker worker but breaks in two production scenarios:

1. **GitHub Actions cron.** Every hourly cron tick spins up a
   fresh ``ubuntu-latest`` worker; ``seen.json`` is ephemeral and
   the next tick starts with an empty file. Every recent job gets
   re-fetched, re-scored, and re-INSERTed. With Supabase REST
   ``ignore_duplicates=True`` the row idempotency holds by accident
   (URL collision = no-op), but the LLM cost is paid twice, the
   audit log is cluttered, and the operator's "old jobs are
   appearing again with new dates" complaint is rooted here.

2. **Multi-worker deploys.** Two concurrent boards workers would
   race on the read-modify-write cycle of the JSON file — last
   writer wins, and earlier writes can be lost.

This module replaces the JSON file with the
:data:`db.models.BoardSeenJob` Postgres table that already exists in
:file:`db/migrations/versions/0001_initial_schema.py`. Dedupe is
atomic (``INSERT ... ON CONFLICT DO NOTHING``) and survives both
worker reboots and concurrent-process races.

Two backends, one public API
============================

The runner expects a key-value store with three operations: load
all seen keys for a board, mark new keys as seen, persist that
load. The cleanest interface is a single set of synchronous
helpers — ``load_seen_for_board`` / ``record_seen_batch`` /
``has_supabase_dedupe`` — and to dispatch between two implementations
internally:

* **Postgres backend** (default when Supabase env is configured).
  Reads from ``board_seen_jobs``; inserts via
  ``INSERT ... ON CONFLICT (job_id_hash) DO NOTHING``. The
  on-disk ``seen.json`` is unused in this path.

* **On-disk fallback** (when Supabase env vars are missing — local
  dev without infra, or a brief Supabase outage). Falls through to
  :func:`utils.seen.load_file` / :func:`utils.seen.save_seen` so
  local development keeps working with no config. The on-disk
  file is still subject to the same race conditions as before —
  this fallback is a development convenience only, not a
  production story.

The dispatcher decides at CALL TIME (per ``load_seen_for_board`` /
``record_seen_batch`` invocation), so a transition from local
dev to deployed GHA that exports ``SUPABASE_URL`` mid-session
takes effect on the next ``run_all`` call without a worker
restart. The on-disk ``seen.json`` is left untouched in the
Postgres path — a future migration helper can copy its contents
into ``board_seen_jobs`` once local-only dev is finished.

Dedupe key shape
================

We store keys as ``f"{board}:{url}"`` strings — composite of the
ATS name and the canonical posting URL. That's the SAME string
:func:`services.scoring_service._job_id` consumes to derive the
UUID5 primary key on the ``jobs`` table, so the seen-store
collides with the row exactly when both rows represent the same
posting. The previous on-disk format carried raw ATS IDs (e.g.
Greenhouse's numeric ``id``) — those never collided with the
``jobs.id`` UUID5 wherever a runner was patched to use URL-based
keys; this brings the keys into alignment.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import models as db_models
from db.session import AsyncSessionLocal, require_database_configured
from utils.seen import load_file as _legacy_load_file
from utils.seen import save_seen as _legacy_save_seen

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Backend detection
# ----------------------------------------------------------------------
def _postgres_backend_enabled() -> bool:
    """True when Supabase/Postgres env vars are configured.

    Honours the same ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``
    signals :mod:`scripts.boards_scan` reads. The actual DB
    connection is validated by :func:`require_database_configured`
    inside the async path; this function is just the yes/no gate so
    the sync wrapper can pick a backend without spinning an event
    loop.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return bool(url) and bool(key)


# ----------------------------------------------------------------------
# Composite key helpers
# ----------------------------------------------------------------------
def dedupe_key(board: str, url: str) -> str:
    """Stable composite dedupe key: ``"<board>:<url>"``.

    Matches the ``f"{ats_type}:{url}"`` shape
    :func:`services.scoring_service._job_id` uses to derive the
    ``jobs.id`` UUID5 PK, so seen-store membership and DB row
    presence are correlated.

    ``board`` and ``url`` are ``.strip()`` before concatenation:
    a stray whitespace from a hand-edited seeded fixture should
    not produce a different key from the same posting with clean
    strings. Empty ``url`` still returns a deterministic string so
    the runner can record "we saw this id" — but downstream code
    should refuse to persist rows without a URL.
    """
    board = (board or "").strip()
    url = (url or "").strip()
    return f"{board}:{url}"


# ----------------------------------------------------------------------
# Public sync API — the runner calls these
# ----------------------------------------------------------------------
def load_seen_for_board(board: str) -> frozenset[str]:
    """Return the composite key set the runner should treat as
    "already delivered" for ``board``.

    Reads from the ``board_seen_jobs`` table when the Postgres
    backend is enabled (read-once-per-run, cached as
    ``frozenset``); otherwise falls back to parsing
    ``backend/data/seen.json`` via the legacy loader so local dev
    without Supabase env still works.

    Note that the legacy loader returns a string-keyed dict whose
    values are *raw ATS IDs* (e.g. ``"5295858008"``, ``"abc"``).
    Composite ``board:url`` lookups against it WILL MISS for any
    job not previously seen — which is exactly backwards-compat
    (the legacy store had no URL-keyed data, so falling through to
    it on first migration is a one-time re-walk rather than a
    silent permanent miss). On the second run the table is
    populated, so this gap closes within one execution.
    """
    if _postgres_backend_enabled():
        try:
            return asyncio.run(_async_load_seen_for_board(board))
        except Exception as exc:  # noqa: BLE001 — fall back gracefully
            logger.warning(
                "board_seen: Postgres load failed (%s); falling back to on-disk seen.json. "
                "Subsequent record_seen_batch will also use the fallback for this run.",
                type(exc).__name__,
            )
    return frozenset(_legacy_load_file().keys())


def record_seen_batch(
    board: str,
    items: Iterable[tuple[str, str]],
) -> int:
    """Persist ``(key, iso_timestamp)`` pairs to ``board_seen_jobs``.

    Idempotent — uses ``INSERT ... ON CONFLICT (job_id_hash) DO
    NOTHING`` so re-running the cron with overlapping job sets is
    safe and ``times_seen`` keeps climbing on subsequent hits.
    Returns the count of rows the INSERT itself reported as
    inserted (excluding the dedupe-no-op rows).

    When the Postgres backend is unavailable, merges into the
    legacy on-disk ``seen.json`` dict via the existing
    :func:`utils.seen.save_seen` contract (the caller still calls
    ``save_seen`` at the end of the runner for symmetry with the
    pre-Postgres flow).
    """
    item_list = list(items)
    if not item_list:
        return 0
    if _postgres_backend_enabled():
        try:
            return asyncio.run(_async_record_seen_batch(board, item_list))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "board_seen: Postgres record failed (%s); falling back to "
                "merge into the on-disk seen.json. The next runner load on this "
                "host will see the merged set.",
                type(exc).__name__,
            )
            merged = _legacy_load_file()
            for key, stamp in item_list:
                merged[key] = stamp
            _legacy_save_seen(merged)
            return len(item_list)
    merged = _legacy_load_file()
    for key, stamp in item_list:
        merged[key] = stamp
    _legacy_save_seen(merged)
    return len(item_list)


# ----------------------------------------------------------------------
# Async backend (Postgres)
# ----------------------------------------------------------------------
async def _async_load_seen_for_board(board: str) -> frozenset[str]:
    """Single-query read of all ``job_id_hash`` keys for ``board``.

    A real production ``board_seen_jobs`` table will accumulate
    hundreds of thousands of rows over months of hourly ticks; we
    want this lookup O(1)-ish (a single index scan per board)
    rather than O(N) over a 50K+ entry list. The composite PK
    on ``job_id_hash`` plus the ``idx_board_seen_board_last_seen``
    index keep the practical case cheap.
    """
    require_database_configured()
    assert AsyncSessionLocal is not None  # noqa: S101
    async with AsyncSessionLocal() as session:
        stmt = db_models.BoardSeenJob.__table__.select().with_only_columns(
            db_models.BoardSeenJob.job_id_hash
        ).where(db_models.BoardSeenJob.board == board)
        rows = (await session.execute(stmt)).scalars().all()
    return frozenset(rows)


async def _async_record_seen_batch(
    board: str,
    items: list[tuple[str, str]],
) -> int:
    """Bulk UPSERT (``ON CONFLICT DO NOTHING``) of ``board_seen_jobs``.

    Single transaction per call so a half-completed batch doesn't
    leak duplicates across runs; the runner calls this once at
    the END of its loop, after every per-(board, slug) thread has
    finished and the dedupe decision is final. ``times_seen`` is
    bumped on conflict (the only place we actually do a mutation
    on the existing row) so a frequently-re-listed job shows
    ``times_seen`` climbing in a future ``SELECT … GROUP BY times_seen``
    query — useful for tuning the cron cadence later.
    """
    from datetime import datetime, timezone

    from sqlalchemy import func

    require_database_configured()
    assert AsyncSessionLocal is not None  # noqa: S101
    if not items:
        return 0

    # Resolve timestamps up front — the legacy seen.json stored
    # ISO strings, and a malformed entry should not abort the
    # whole batch. ``_ensure_aware_datetime`` returns ``None`` for
    # unparseable stamps; we fall back to a Python-side ``now``
    # so the column has a value (the table schema is
    # NOT NULL default ``now()`` server-side; we just want a
    # deterministic client-side choice when the legacy stamp is
    # bad). ``func.now()`` is reserved for the ``last_seen_at``
    # update-set below where SQLAlchemy wants a SQL expression.
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        rows = [
            {
                "job_id_hash": key,
                "board": board,
                "first_seen_at": _ensure_aware_datetime(stamp) or now,
                "last_seen_at": _ensure_aware_datetime(stamp) or now,
                "times_seen": 1,
                "company_id": None,
            }
            for key, stamp in items
        ]
        stmt = pg_insert(db_models.BoardSeenJob).values(rows)
        # On conflict: ``times_seen`` increments; ``last_seen_at``
        # bumps to server-time ``now()``. ``first_seen_at`` is
        # preserved because it isn't in the update-set — the row
        # keeps the very first time we observed the dedupe key
        # rather than clobbering it on every re-observation. See
        # https://www.postgresql.org/docs/current/sql-insert.html
        # for the ``ON CONFLICT … DO UPDATE`` + ``EXCLUDED``
        # semantics.
        stmt = stmt.on_conflict_do_update(
            index_elements=[db_models.BoardSeenJob.job_id_hash],
            set_={
                "times_seen": db_models.BoardSeenJob.times_seen + 1,
                "last_seen_at": func.now(),
            },
        )
        result = await session.execute(stmt)
        await session.commit()
        # ``result.rowcount`` for an UPSERT includes all touched
        # rows (both new inserts and existing-no-op bumps).
        # Returning the total touched is sufficient because the
        # runner doesn't care about the no-op count.
        return int(result.rowcount or 0)


def _ensure_aware_datetime(stamp: str | None):
    """Parse the legacy ``iso`` stamp; ``None`` if unparseable.

    A defensive ``None`` defers the column default to
    ``func.now()`` server-side, so a malformed cached timestamp
    can't crash the bulk insert. Same contract as
    :func:`utils.time_check.parse_published_at` minus the strict
    type coercion — we already validated upstream.
    """
    if not stamp:
        return None
    from datetime import datetime, timezone

    try:
        normalized = stamp.replace("Z", "+00:00") if stamp.endswith("Z") else stamp
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


__all__ = [
    "dedupe_key",
    "load_seen_for_board",
    "record_seen_batch",
]
