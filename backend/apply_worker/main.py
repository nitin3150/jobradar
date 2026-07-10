"""apply_worker.main — orchestration loop for the auto-apply worker.

Long-running asyncio loop. On each tick:

1. Open a session.
2. ``SELECT ... WHERE status='approved' ORDER BY created_at ASC
   LIMIT 1 FOR UPDATE SKIP LOCKED`` — FIFO fairness + race-safe
   multi-worker support (two worker dynos can't both claim the
   same row).
3. Fetch the latest ``resumes`` + ``qa_bank_entries`` snapshots
   (only on ticks that have a row — see ``run_one_tick``'s IDLE
   short-circuit; an empty-queue tick avoids the wasted reads).
4. Run :func:`apply_worker.resume_picker.pick_resume`.
5. Run :func:`apply_worker.qa_matcher.match_questions` against
   the placeholder ``[]`` field list — the real
   :func:`form_filler.fill_form` will populate it once Playwright
   lands; today the matcher sees "0 fields" and returns ``[]``.
6. On a deterministic failure (no resume pickable / a match with
   ``entry_id is None``) the row is atomically flipped
   ``approved → paused`` so it surfaces in the React
   ``PendingReviewWidget`` "Paused" sub-list. Operator-side
   recovery is the existing **Resume** button — once the
   operator uploads a missing resume or fills the missing Q&A,
   ``Resume`` flips ``paused → approved`` and the row re-enters
   the deque. This avoids an unbounded retry-counter column on
   the ``jobs`` table — the ``paused`` enum value carries the
   same semantics with no schema change.
7. On success: atomically flip ``approved → applied``, insert an
   :class:`db.models.Application` row with the same submit
   metadata, append a :class:`db.models.JobStatusHistory` row
   tagged ``source='auto_apply'`` so the audit trail distinguishes
   programmatic transitions from operator clicks (the latter carry
   ``source='user'``).
8. The ``FOR UPDATE SKIP LOCKED`` lock auto-releases at
   ``session.commit()`` so the next tick — or any other worker
   dyno — sees fresh rows.

Graceful shutdown
=================

Render / Docker stop sends ``SIGTERM`` to the dyno; Ctrl-C is
``SIGINT``. Both are wired through
:func:`asyncio.loop.add_signal_handler` to set a shared
:class:`asyncio.Event`; the poll loop checks the event AFTER
each tick + AFTER each ``wait_for`` so SIGTERM during a 30 s
sleep exits within a few hundred ms rather than waiting the
full interval. An in-flight ``run_one_tick`` (``async with
session.begin()``) ALWAYS completes before exit so we never
leave a row locked + half-flipped.

Operational model
=================

The worker is one process per dyno. Two replicas running
concurrently will see ``FOR UPDATE SKIP LOCKED`` divide the
work — the second worker simply waits for the first's
transaction to complete before claiming its own row, with no
double-submit risk.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apply_worker.form_filler import fill_form as default_fill_form
from apply_worker.resume_picker import pick_resume
from apply_worker.types import MatchResult, ResumeRecord
from db import models as db_models
from db.audit import record_status_history
from services.llm_client import LLMClient


_logger = logging.getLogger("jobradar.apply_worker")


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

# 30 s between ticks. The user spec. Operators can override via env
# (useful for staging ramps) without rewriting the loop — read at
# process start.
DEFAULT_TICK_INTERVAL_SECONDS = 30
TICK_INTERVAL_SECONDS = int(
    os.environ.get("APPLY_WORKER_TICK_SECONDS", str(DEFAULT_TICK_INTERVAL_SECONDS))
)

# Per-tick fetch size. ``1`` keeps the per-tick load bounded and the
# lock-window short; multi-dyno parallelism comes from multiple
# workers running this loop concurrently (``FOR UPDATE SKIP LOCKED``
# is the multi-worker-friendly primitive here). Operators can bump
# via env if their queue is huge and the dyno count is low.
JOB_FETCH_LIMIT = 1

# Backoff after an unhandled tick error so a sustained DB outage
# doesn't hammer the connection pool. Operators can override via env.
DEFAULT_ERROR_BACKOFF_SECONDS = 5
ERROR_BACKOFF_SECONDS = int(
    os.environ.get("APPLY_WORKER_ERROR_BACKOFF_SECONDS", str(DEFAULT_ERROR_BACKOFF_SECONDS))
)


# ----------------------------------------------------------------------
# Outcome type — drives both logs and tests. Each tick surfaces a
# single ``TickOutcome`` so the caller (tests + the loop's main
# statistics path) can dispatch on a clean enum rather than guessing
# from log lines.
# ----------------------------------------------------------------------


class TickStatus(str, Enum):
    IDLE = "idle"  # queue is empty (no approved rows)
    NO_RESUME = "no_resume"  # picker returned None → parked
    NO_FIELDS = "no_fields"  # qa_matcher saw 0 fields → parked (currently unreachable)
    UNMATCHED_FIELDS = "unmatched_fields"  # qa_matcher saw ≥1 source='none' → parked
    SUBMITTED = "submitted"  # happy path: approved → applied
    PAUSED_RACE = "paused_race"  # row was paused mid-tick (operator veto) → no-op commit


@dataclass(slots=True)
class TickOutcome:
    """Result of one tick — drives tests + log lines + exit code."""

    status: TickStatus
    job_id: str | None = None
    detail: str = ""
    # Wall-clock seconds spent on this tick (session open + dequeue +
    # picker + matcher + write + session close). The orchestrator
    # logs it on each iteration; tests assert the tick was bounded
    # by their mock's response time, not by the 30 s sleep.
    elapsed_seconds: float = 0.0


# ----------------------------------------------------------------------
# Helper — fetch all resumes + qa bank rows for THIS tick. Refreshed
# per row (small cost, big UX win when an operator edits mid-batch).
# Each is a single indexed SELECT.
# ----------------------------------------------------------------------


async def _fetch_resumes_and_bank(
    session: AsyncSession,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-tick snapshot — the operator's mid-batch edits land on the
    next row, not after a worker restart.

    Returns ``(resumes_as_plain_dicts, qa_bank_entries_as_plain_dicts)``
    in the shape ``apply_worker.types`` ``from_data`` constructors can
    ingest directly. Plain-dict keeps the dataclass boundary one
    layer deep (``from_data`` is a thin dict-reader), and matches
    the contract :func:`apply_worker.resume_picker.pick_resume` /
    :func:`apply_worker.qa_matcher.match_questions` advertise.
    """
    resumes_rows = (
        await session.execute(
            select(db_models.Resume).order_by(db_models.Resume.uploaded_at.desc())
        )
    ).scalars().all()
    qa_rows = (
        await session.execute(
            select(db_models.QABankEntry).order_by(db_models.QABankEntry.times_used.desc())
        )
    ).scalars().all()
    return (
        [
            {
                "id": str(r.id),
                "name": r.name,
                "tags": list(r.tags or []),
                "is_default": bool(r.is_default),
                "uploaded_at": _iso_or_empty(r.uploaded_at),
                "storage_path": r.storage_path,
            }
            for r in resumes_rows
        ],
        [
            {
                "id": str(e.id),
                "question_pattern": e.question_pattern,
                "canonical_question": e.canonical_question,
                "answer": e.answer,
                "answer_type": e.answer_type,
                "times_used": int(e.times_used or 0),
            }
            for e in qa_rows
        ],
    )


def _iso_or_empty(dt: datetime | None) -> str:
    """Render a :class:`datetime` as ISO 8601 with trailing ``Z``; empty string for None.

    Mirrors the ``+00:00`` → ``Z`` rewrite used across the wire shape
    so :func:`apply_worker.resume_picker._timestamp_key` parses the
    result cleanly. ``None`` is mapped to ``""`` rather than the
    literal ``"None"`` so the picker's tiebreak sorts empty uploads
    last rather than first (lex order).
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------------------
# Core — one dequeue-and-process iteration. Tests drive this directly
# with a mocked ``session`` (``AsyncMock``), ``llm_client`` (mocked),
# and ``form_filler`` (mocked). Production's :func:`main_loop` calls
# it once per tick.
# ----------------------------------------------------------------------


async def run_one_tick(
    session: AsyncSession,
    llm_client: LLMClient,
    *,
    form_filler: Callable[..., Awaitable[list[dict[str, Any]]]] = default_fill_form,
) -> TickOutcome:
    """Process at most :data:`JOB_FETCH_LIMIT` row(s); return a :class:`TickOutcome`.

    Single coroutine — keeps ``session.begin()`` open for the entire
    deque-+-process-+-write cycle so the ``FOR UPDATE SKIP LOCKED``
    lock auto-releases only at the final commit/rollback. No tick-
    internal sub-commit or session.close mid-way.

    Sub-routines are split out as ``_park_for_operator_review`` and
    ``_finalize_apply`` (both async) so the control flow above stays
    readable.
    """
    started = datetime.now(timezone.utc)
    async with session.begin():
        # 1. Dequeue. ``with_for_update(skip_locked=True)`` is the
        # multi-worker-friendly primitive — concurrent claims skip
        # rows another worker already holds. The ORDER BY created_at
        # ASC keeps dequeuing FIFO so a job approved 3 hours ago runs
        # before a job approved 30 s ago.
        stmt = (
            select(db_models.Job)
            .where(db_models.Job.status == "approved")
            .order_by(asc(db_models.Job.created_at))
            .limit(JOB_FETCH_LIMIT)
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            # Empty queue — skip the snapshot fetches (cheap on a
            # busy queue; pure waste on an empty one).
            return TickOutcome(
                status=TickStatus.IDLE,
                elapsed_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            )
        job = rows[0]
        # Defer ``previous_status`` capture until after the
        # PAUSED_RACE early-return so the local's lifetime matches
        # its sole use (audit-trail row for ``approved → applied``).

        # 2. Operator-side race guard. The lock from ``FOR UPDATE``
        # protected us against OTHER workers, but NOT against an
        # operator's PATCH that raced us in via a different session.
        # The Postgres row state at the moment we read it is
        # canonical — if it's ``approved``, the operator can't have
        # changed it (their PATCH would have committed before our
        # lock acquired). If it's anything else (e.g. ``paused`` was
        # set by an out-of-band PATCH on the same row), no-op commit.
        if job.status != "approved":
            return TickOutcome(
                status=TickStatus.PAUSED_RACE,
                job_id=str(job.id),
                detail=f"row reached tick in status={job.status!r}; not applying",
                elapsed_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            )

        # 3. Snapshot resumes + Q&A bank. Per-tick (cheap — two
        # indexed SELECTs) so an operator's mid-dequeue edit lands
        # on the next row rather than waiting for a worker restart.
        resumes_payload, qa_payload = await _fetch_resumes_and_bank(session)

        # 4. Pick resume. Returning ``None`` is deterministic — no
        # tag overlap, no default, and the LLM-fallback path either
        # wasn't supplied or also failed. Park to ``paused`` so the
        # operator gets a Parked sub-list row to fix and re-Resume.
        #
        # :func:`resume_picker.pick_resume` returns
        # ``ResumeRecord | None`` directly (NOT plain dict) — see
        # its signature. No conversion needed in the orchestrator.
        chosen_resume: ResumeRecord | None = await pick_resume(
            {
                "title": job.title,
                "description": job.description or "",
                "ats_type": job.ats_type,
                "company_name": job.company_name,
            },
            resumes_payload,
            llm_client=llm_client,
        )
        if chosen_resume is None:
            await _park_for_operator_review(
                session,
                job,
                reason="no resume matched",
            )
            _logger.info(
                "apply_worker: parked job %s (no resume); deque retry gated by Resume click",
                job.id,
            )
            return TickOutcome(
                status=TickStatus.NO_RESUME,
                job_id=str(job.id),
                detail="no resume matched and no LLM-fallback; parked to paused",
                elapsed_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            )

        # 5. Form-filler call. The real Playwright impl owns the
        # entire per-job session (goto → extract → match_questions
        # → fill → upload resume → submit → screenshot → upload).
        # It returns a 2-tuple ``(events, qa_matches)`` — see
        # :func:`apply_worker.form_filler.fill_form` for the
        # rationale. Splitting events + qa_matches out of one
        # dict return keeps the orchestrator's branching logic
        # type-safe (``list[MatchResult]`` not ``list[dict]``) and
        # keeps :func:`match_questions` from being called twice
        # (once here, once inside form_filler).
        events, qa_matches = await form_filler(
            job={
                "id": str(job.id),
                "title": job.title,
                "company_name": job.company_name,
                "url": job.url,
                "ats_type": job.ats_type,
                "description": job.description or "",
                "ai_fit_reasoning": job.ai_fit_reasoning or "",
                "created_at": _iso_or_empty(job.created_at),
            },
            resume={
                "id": chosen_resume.id,
                "name": chosen_resume.name,
                "tags": chosen_resume.tags,
                "is_default": chosen_resume.is_default,
                "uploaded_at": chosen_resume.uploaded_at,
                "storage_path": chosen_resume.storage_path,
            },
            bank=qa_payload,
            llm_client=llm_client,
        )
        # ``events = []`` ⇒ form_filler parked the row itself:
        #   * ``qa_matches = []`` — page had no fields to extract
        #     (``goto`` worked but the form has zero inputs).
        #     Orchestrator parks with :data:`TickStatus.NO_FIELDS`.
        #   * ``qa_matches != []`` with at least one ``entry_id is
        #     None`` — form_filler EARLY-ABORTED before clicking
        #     submit because submitting a half-filled ATS form
        #     would be worse than parking. The orchestrator
        #     surfaces this with :data:`TickStatus.UNMATCHED_FIELDS`
        #     so the operator sees the unmatched count + label list
        #     in the PendingReviewWidget's "Paused" sub-list.
        if not events and not qa_matches:
            await _park_for_operator_review(
                session,
                job,
                reason="form_filler: page had no form fields to extract",
            )
            return TickOutcome(
                status=TickStatus.NO_FIELDS,
                job_id=str(job.id),
                detail="form_filler found 0 form fields; parked",
                elapsed_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            )
        if not events:
            unmatched_count = sum(1 for m in qa_matches if m.entry_id is None)
            await _park_for_operator_review(
                session,
                job,
                reason=f"qa matcher left {unmatched_count} field(s) unmatched; "
                       f"form_filler aborted before submit",
            )
            return TickOutcome(
                status=TickStatus.UNMATCHED_FIELDS,
                job_id=str(job.id),
                detail=(
                    f"form_filler early-aborted before submit; "
                    f"{unmatched_count}/{len(qa_matches)} field(s) unmatched"
                ),
                elapsed_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            )

        # 7. Happy-path write. Flip status + INSERT Application +
        # audit row, all in the same transaction so an audit query
        # can never see ``status='applied'`` without a matching
        # history row.
        # Capture ``previous_status`` here (vs. right after the
        # deque) so the local's lifetime matches its sole use site —
        # the audit-trail row for ``approved → applied``.
        previous_status = job.status
        applied = await _finalize_apply(
            session,
            job=job,
            resume=chosen_resume,
            qa_matches=qa_matches,
            form_filler_events=events,
            previous_status=previous_status,
        )
        _logger.info(
            "apply_worker: applied job %s (resume=%s, matches=%d)",
            job.id,
            chosen_resume.id,
            len(qa_matches),
        )
        return TickOutcome(
            status=TickStatus.SUBMITTED,
            job_id=str(job.id),
            detail=f"approved → applied, application_id={applied.id}",
            elapsed_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
        )


# ----------------------------------------------------------------------
# Internal helpers — keep ``run_one_tick`` readable by extracting
# the two write paths.
# ----------------------------------------------------------------------


async def _park_for_operator_review(
    session: AsyncSession,
    job: db_models.Job,
    *,
    reason: str,
) -> None:
    """Flip ``approved → paused`` + append an audit row.

    Caller MUST be inside ``session.begin()``. The row's
    ``previous_status`` (``approved`` at dequeue time) is recorded
    on the :class:`db.models.JobStatusHistory` side so an analyst
    query can recover the exact sequence: ``scorer wrote
    'approved'`` → ``worker couldn't pick resume / match fields,
    parked 'paused'``. Operator's ``Resume`` button will write
    ``paused → approved`` and the row re-enters the deque.

    Async so callers can ``await`` it within an ``async with
    session.begin():`` block; the body itself is sync (no awaits
    today) but the signature matches the orchestrator's caller.
    """
    previous_status = job.status
    job.status = "paused"
    record_status_history(
        session,
        job.id,
        previous_status,
        "paused",
        db_models.JOB_STATUS_SOURCE_AUTO_APPLY,
        note=f"parked by apply_worker: {reason}",
    )


async def _finalize_apply(
    session: AsyncSession,
    *,
    job: db_models.Job,
    resume: ResumeRecord,
    qa_matches: list[MatchResult],
    form_filler_events: list[dict[str, Any]],
    previous_status: str,
) -> db_models.Application:
    """Flip ``approved → applied`` + INSERT Application + audit row, atomically.

    Mirrors :func:`routes.applications.create_application_from_job` (the
    operator's manual-apply endpoint) but writes
    ``source='auto_apply'`` rather than ``source='user'`` so a
    single ``SELECT to_status, source FROM job_status_history GROUP
    BY 1, 2`` surfaces programmatic vs human transitions.

    ``submission_screenshot_path`` is ``None`` today (no real
    Playwright run) — the apply_worker (not the manual path) is
    the only writer of that column. Future: the :func:`form_filler`
    event-list carries a ``screenshot_path`` that we copy here.

    Async because we ``await session.flush()`` after the
    :class:`db.models.Application` ``session.add()`` so the FK
    constraint on ``job_id`` is satisfied at INSERT time before
    we flip the parent :class:`db.models.Job` row's status.
    """
    now = datetime.now(timezone.utc)

    # Pick the screenshot path from the form_filler events when the
    # real Playwright impl lands; today the stub returns
    # ``{"screenshot_path": None}`` so this stays NULL.
    screenshot_path: str | None = None
    for ev in form_filler_events:
        candidate = ev.get("screenshot_path")
        if candidate:
            screenshot_path = str(candidate)
            break

    application_row = db_models.Application(
        # FK to the job we just dequeued — kept so a future
        # JobBoardStatusDropdown can show "applied via auto-apply
        # at <timestamp>" trail.
        job_id=job.id,
        job_title=job.title,
        company_name=job.company_name,
        submitted_at=now,
        status="submitted",
        last_email_at=None,
        submission_screenshot_path=screenshot_path,
        notes=(
            f"auto-apply ticket; resume={resume.id}; "
            f"qamatches={'|'.join(m.entry_id or 'None' for m in qa_matches) or 'none'}"
        ),
    )
    session.add(application_row)

    # Flush BEFORE flipping Job.status so the FK constraint on
    # ``job_id`` is satisfied at INSERT time. Without this, the FK
    # could race a stale row read.
    await session.flush()

    job.status = "applied"
    record_status_history(
        session,
        job.id,
        previous_status,
        "applied",
        db_models.JOB_STATUS_SOURCE_AUTO_APPLY,
        note=f"auto-apply (resume={resume.id})",
    )
    return application_row


# ----------------------------------------------------------------------
# Long-running loop. ``run_one_tick`` is the testable unit; this is
# the prod entry point. ``async with session.begin()`` is kept
# inside ``run_one_tick`` so a single tick is atomic; the outer
# ``async with AsyncSessionLocal() as session:`` lifecycle just
# owns connection-pool checkout + return.
# ----------------------------------------------------------------------


async def main_loop(
    *,
    llm_client: LLMClient | None = None,
    tick_seconds: int = TICK_INTERVAL_SECONDS,
    error_backoff_seconds: int = ERROR_BACKOFF_SECONDS,
    form_filler: Callable[..., Awaitable[list[dict[str, Any]]]] = default_fill_form,
) -> None:
    """Run the poll loop until SIGTERM/SIGINT.

    Constructor pattern: a fresh :class:`LLMClient` is built at
    startup (cached inside the client) and reused across ticks —
    cheaper than rebuilding per tick, and lets the underlying
    ``AsyncOpenAI`` clients warm their connection pool on the
    first call. Tests can pass a mocked ``llm_client``.

    ``tick_seconds`` and ``error_backoff_seconds`` are configurable
    (operators floor them for staging ramps via env). The shutdown
    event is set by signal handlers installed ONCE at startup so
    SIGTERM during a sleep exits promptly rather than waiting the
    full interval.
    """
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # ``add_signal_handler`` only works on Unix-like loops; on
        # Windows it raises NotImplementedError. We don't run on
        # Windows in production but bail softly to a no-op so a
        # Windows dev's Ctrl-C still raises KeyboardInterrupt
        # (asyncio's default).
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, RuntimeError):
            _logger.warning(
                "apply_worker: add_signal_handler not supported on this platform; "
                "Ctrl-C will raise KeyboardInterrupt before the next tick boundary"
            )

    llm_client = llm_client or LLMClient.from_env()
    _logger.info("apply_worker: starting (tick=%ds, backoff=%ds)", tick_seconds, error_backoff_seconds)

    while not shutdown_event.is_set():
        # One connection per tick — open wide so the FOR UPDATE SKIP
        # LOCKED row is held for the shortest possible window. The
        # sessionmaker's pool reuses connections across ticks for
        # cheap reconnection.
        from db.session import AsyncSessionLocal, require_database_configured

        require_database_configured()
        assert AsyncSessionLocal is not None  # noqa: S101
        async with AsyncSessionLocal() as session:
            try:
                outcome = await run_one_tick(
                    session, llm_client, form_filler=form_filler
                )
                _logger.info(
                    "apply_worker: tick status=%s job_id=%s elapsed=%.3fs",
                    outcome.status.value,
                    outcome.job_id or "<none>",
                    outcome.elapsed_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                # ANY tick error must not break the loop. The session
                # is exited via the ``async with`` block (which
                # rolls back uncommitted tx). The next tick opens a
                # fresh session and tries again.
                _logger.exception(
                    "apply_worker: tick raised %s; will retry after backoff",
                    type(exc).__name__,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=error_backoff_seconds)
                except asyncio.TimeoutError:
                    pass
                continue

        if shutdown_event.is_set():
            break
        # Interruptible sleep. SIGTERM during a 30 s ``asyncio.sleep``
        # would otherwise wait the full interval; ``wait_for`` flips
        # the wakeup to instant.
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass

    _logger.info("apply_worker: shutdown complete")


__all__ = [
    "TickOutcome",
    "TickStatus",
    "run_one_tick",
    "main_loop",
    "TICK_INTERVAL_SECONDS",
    "JOB_FETCH_LIMIT",
    "ERROR_BACKOFF_SECONDS",
]


if __name__ == "__main__":
    # Entry point for ``python -m apply_worker.main`` AND direct
    # ``python backend/apply_worker/main.py`` runs. ``uvicorn``-style
    # pattern from ``backend/main.py`` mirrored here — no FastAPI
    # app, just the worker loop.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(main_loop())
