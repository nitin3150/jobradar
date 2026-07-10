"""Tests for :mod:`backend.scripts.apply_worker_tick` — exercises the
GHA-cron drain loop without a live Postgres database.

Why a custom mock harness (vs. ``AsyncMock`` end-to-end)
=======================================================

The drain loop is a GHA-facing CLI: every cron tick opens ONE SQLAlchemy
session, calls :func:`apply_worker.main.run_one_tick` against it, and
repeats until IDLE OR a wall-clock deadline fires. We mock at three
contract seams the tick script's :func:`_drain_loop` advertises:

* ``session_factory`` — defaults to :data:`db.session.AsyncSessionLocal`;
  tests pass a custom asynccontextmanager that yields an
  :class:`unittest.mock.AsyncMock` so each tick iteration's
  ``async with session_factory() as session:`` succeeds without any
  real DB connectivity. Mirrors the per-tick harness in
  :mod:`tests.test_apply_worker_main` so the orchestrator's tuple-
  return contract doesn't need rewriting here.
* ``llm_factory`` — defaults to :func:`services.llm_client.LLMClient.from_env`;
  tests pass a no-op factory so the drain loop doesn't need real
  ``NVIDIA_API_KEY`` / ``GROQ_API_KEY`` env vars. The ``_drain_loop``
  calls the factory ONCE per invocation (not per tick) so re-using a
  stub is cheap.
* ``tick_runner`` — defaults to :func:`apply_worker.main.run_one_tick`;
  tests pass a callable that returns pre-canned :class:`TickOutcome`
  values via :class:`AsyncMock` ``side_effect`` to drive the IDLE /
  SUBMITTED / NO_FIELDS / UNMATCHED_FIELDS branches without a real
  ORM. The ``AsyncMock`` accepts any positional/keyword args so the
  orchestrator's exact signature doesn't need matching at this layer.

Why a stub ``DATABASE_URL`` instead of monkeypatching
:data:`AsyncSessionLocal` to ``None``
=====================================================

:func:`_drain_loop` calls :func:`db.session.require_database_configured`
on the FIRST line, BEFORE the ``session_factory`` parameter is even
referenced. The check exists to surface the "operator forgot to set
DATABASE_URL in GHA secrets" error early (so the failure-email arrives
in seconds, not after the 100-s setup), but it forces the unit tests
to ALSO pre-populate ``DATABASE_URL`` even when the test overrides
``session_factory``. Solution: stub URL + ``JOBRADAR_TEST_DB=1`` →
asyncpg constructs a NullPool engine that never opens a connection
(because the override ``session_factory`` bypasses engine entirely).
The real connection pool never gets used.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

# Stub env vars BEFORE importing the tick script so
# ``db.session.AsyncSessionLocal`` constructs an engine. ``NullPool``
# (via ``JOBRADAR_TEST_DB=1`` set by :mod:`conftest`) means the engine
# never opens a real connection — the test's ``session_factory``
# override bypasses the engine entirely.
_STUB_DATABASE_URL = "postgresql+asyncpg://stub:stub@localhost:1/stub"
os.environ.setdefault("DATABASE_URL", _STUB_DATABASE_URL)

# Stub LLM key so ``LLMClient.from_env`` could construct if a test
# forgets to override ``llm_factory``. The conftest already sets the
# test DB flag; the script import path passes.
os.environ.setdefault("GROQ_API_KEY", "stub-groq-key-for-tests-only")

# ``apply_worker_tick.py`` lives at backend/scripts/ — NOT under a
# package because ``scripts/`` doesn't have ``__init__.py`` and
# isn't listed in :mod:`pyproject.toml`'s ``packages`` (boards_scan
# CLI follows the same run-to-completion-script convention). The
# simplest test-side setup is to put backend/scripts/ on sys.path so
# ``import apply_worker_tick`` resolves the file by its basename.
# The tick script's own top-of-file ``sys.path.insert`` of
# ``backend/`` keeps ``from apply_worker.main import ...`` working
# once the test imports the module.
_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_SCRIPTS_DIR = os.path.join(_BACKEND_ROOT, "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

# Imported AFTER the sys.path inserts above. Module-load runs the
# tick script's top-level imports (apply_worker.main, db.session,
# services.llm_client) — none of which fail at import time as long
# as a stub DATABASE_URL is present (engine constructs, no real
# connection opened thanks to NullPool).
import apply_worker_tick as tick_mod  # noqa: E402

from apply_worker.main import TickOutcome, TickStatus  # noqa: E402


# ---------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------


@asynccontextmanager
async def _fake_session_factory():
    """A ``session_factory`` override that yields a fresh
    :class:`AsyncMock` so each ``async with session_factory() as
    session:`` iteration in the drain loop succeeds without any
    real DB connectivity. Mirrors the per-tick harness in
    :mod:`tests.test_apply_worker_main`."""
    yield AsyncMock()


def _stub_llm_factory() -> AsyncMock:
    """No-op LLMClient stub. The drain loop accepts it without
    exercising it when the tick_runner returns pre-canned outcomes."""
    return AsyncMock()


def _outcome_runner(outcomes: list[TickOutcome]) -> AsyncMock:
    """Turn a static list of :class:`TickOutcome` into an
    :class:`AsyncMock` ``side_effect`` that returns each in turn.

    AsyncMock treats a ``side_effect=`` list as a queue — first call
    returns ``outcomes[0]``, second returns ``outcomes[1]``, etc.
    An :class:`AsyncMock` is a coroutine when awaited, so
    ``await tick_runner(...)`` consumes one side_effect item per
    call without needing explicit ``__call__`` plumbing.

    Tests that exhaust the list raise ``StopIteration`` from the
    AsyncMock; the drain loop's session.close() rolls back so the
    failure is non-corrupting, but a CI run will surface the
    traceback clearly if a test mis-calibrates the outcome count.
    """
    return AsyncMock(side_effect=list(outcomes))


# ---------------------------------------------------------------------
# Drain-loop logic tests
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_loop_exits_on_idle_with_zero_processed() -> None:
    """Empty queue → exits cleanly with processed=0 after one
    iteration that returns IDLE.

    Pins the contract that ``processed`` counts ROWS processed
    (NOT iterations including the IDLE bail). It's an obvious
    off-by-one bug class in drain loops.
    """
    runner = _outcome_runner(
        [TickOutcome(status=TickStatus.IDLE, elapsed_seconds=0.05)]
    )
    processed = await tick_mod._drain_loop(
        timeout_seconds=30,
        session_factory=_fake_session_factory,
        llm_factory=_stub_llm_factory,
        tick_runner=runner,
    )
    assert processed == 0, "IDLE on iter 1 must not count as 'processed'"
    assert runner.await_count == 1
    assert runner.await_args.args[0] is not None  # session was passed
    assert runner.await_args.args[1] is not None  # llm was passed


@pytest.mark.asyncio
async def test_drain_loop_processes_n_rows_until_idle() -> None:
    """3 non-IDLE rows + 1 IDLE → processed=3 (the IDLE bail is
    not counted). N calls to tick_runner."""
    outcomes = [
        TickOutcome(
            status=TickStatus.SUBMITTED,
            job_id="job-1",
            elapsed_seconds=0.1,
        ),
        TickOutcome(
            status=TickStatus.SUBMITTED,
            job_id="job-2",
            elapsed_seconds=0.1,
        ),
        TickOutcome(
            status=TickStatus.SUBMITTED,
            job_id="job-3",
            elapsed_seconds=0.1,
        ),
        TickOutcome(
            status=TickStatus.IDLE,
            elapsed_seconds=0.05,
        ),
    ]
    runner = _outcome_runner(outcomes)
    processed = await tick_mod._drain_loop(
        timeout_seconds=300,
        session_factory=_fake_session_factory,
        llm_factory=_stub_llm_factory,
        tick_runner=runner,
    )
    assert processed == 3
    assert runner.await_count == 4, (
        "loop must call tick_runner once per row + once for the "
        "IDLE bail; off-by-one here means queue is left dirty"
    )


@pytest.mark.asyncio
async def test_drain_loop_respects_wall_clock_deadline() -> None:
    """Tick takes longer than deadline → loop exits at deadline
    WITHOUT awaiting more ticks.

    The deadline check happens BEFORE each ``async with`` block so
    the in-flight tick is allowed to complete — half-flipping a
    ``FOR UPDATE SKIP LOCKED`` row would be the worse failure
    mode. A single slow tick that pushes time past the deadline
    in-flight is safe (the session.commit() in tick_runner fires
    before the deadline check on iter+1).
    """
    # Sleep delay chosen so the FIRST tick pushes past a 1-second
    # deadline. ``time.monotonic()`` cannot be monkeypatched from
    # outside the loop body without rewriting the module, so the
    # tick-time route is the simplest calibration.
    SLEEP_SECONDS = 2.0
    DEADLINE_SECONDS = 1

    async def slow_tick(_session, _llm) -> TickOutcome:
        await asyncio.sleep(SLEEP_SECONDS)
        return TickOutcome(
            status=TickStatus.SUBMITTED,
            job_id="job-slow",
            elapsed_seconds=SLEEP_SECONDS,
        )

    processed = await tick_mod._drain_loop(
        timeout_seconds=DEADLINE_SECONDS,
        session_factory=_fake_session_factory,
        llm_factory=_stub_llm_factory,
        tick_runner=slow_tick,
    )
    # The single slow tick completed in-flight; the next
    # iteration's deadline check (now ≥ deadline) bailed.
    # ``processed == 1`` confirms the in-flight tick was
    # allowed to commit (NOT killed mid-session.begin()).
    assert processed == 1


@pytest.mark.asyncio
async def test_drain_loop_propagates_unhandled_tick_exception() -> None:
    """Unhandled tick exception propagates out of ``_drain_loop``
    so the ``if __name__ == "__main__"`` block's ``sys.exit(1)``
    fires and GHA surfaces a failure-email.

    The ``async with session_factory() as session:`` context manager
    in the loop body is responsible for rolling back the FOR UPDATE
    SKIP LOCKED row on the way out — the fakes don't implement
    rollback explicitly, but the test confirms the exception
    propagation path that lets subprocess ``sys.exit(1)`` land.
    """
    runner = AsyncMock(
        side_effect=RuntimeError("asyncpg connection refused")
    )

    with pytest.raises(RuntimeError, match="asyncpg connection refused"):
        await tick_mod._drain_loop(
            timeout_seconds=30,
            session_factory=_fake_session_factory,
            llm_factory=_stub_llm_factory,
            tick_runner=runner,
        )


@pytest.mark.asyncio
async def test_drain_loop_count_parked_rows_as_processed() -> None:
    """TickStatus.{NO_RESUME, NO_FIELDS, UNMATCHED_FIELDS,
    PAUSED_RACE} all count as "processed" because they each:
    (a) consumed a row from the FOR UPDATE SKIP LOCKED queue, and
    (b) flipped the row's status (parked-or-no-op).

    This pins the contract that operator-side parking events
    don't leak queue rows back to the next cron tick.
    """
    outcomes = [
        TickOutcome(status=TickStatus.NO_RESUME, job_id="job-no-resume",
                    detail="parked", elapsed_seconds=0.1),
        TickOutcome(status=TickStatus.NO_FIELDS, job_id="job-no-fields",
                    detail="parked", elapsed_seconds=0.1),
        TickOutcome(status=TickStatus.UNMATCHED_FIELDS, job_id="job-unmatched",
                    detail="parked", elapsed_seconds=0.1),
        TickOutcome(status=TickStatus.PAUSED_RACE, job_id="job-raced",
                    detail="parked", elapsed_seconds=0.1),
        TickOutcome(status=TickStatus.IDLE, elapsed_seconds=0.05),
    ]
    runner = _outcome_runner(outcomes)
    processed = await tick_mod._drain_loop(
        timeout_seconds=300,
        session_factory=_fake_session_factory,
        llm_factory=_stub_llm_factory,
        tick_runner=runner,
    )
    # 4 rows consumed (all non-IDLE) + 1 IDLE bail = 5 calls.
    assert processed == 4
    assert runner.await_count == 5


# ---------------------------------------------------------------------
# Config-validation tests
# ---------------------------------------------------------------------


def test_require_env_exits_with_clean_message_on_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_require_env('MISSING')`` exits 1 with a clean remediation
    message rather than a Python traceback.

    It's the operator-facing pre-flight check; a raw traceback
    would obscure which GHA secret needs adding. Pins that the
    exit code is 1 (so GHA's failure-email fires) AND that the
    name appears in the emitted message.
    """
    monkeypatch.delenv("APPLY_TICK_REQUIRED_TEST", raising=False)
    captured = []

    real_print = print

    def capture(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.print", capture)
    with pytest.raises(SystemExit) as exc_info:
        tick_mod._require_env("APPLY_TICK_REQUIRED_TEST")
    assert exc_info.value.code == 1
    # Operator-facing message names the missing env var so the
    # failure-email body is actionable.
    blob = "\n".join(captured)
    assert "APPLY_TICK_REQUIRED_TEST" in blob


def test_validate_llm_keys_exits_when_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both NVIDIA + GROQ unset → ``_validate_llm_keys_present``
    exits 1; GHA's secret-lint catches the same condition in the
    workflow, but the script's own check is the final gate."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY_2", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        tick_mod._validate_llm_keys_present()
    assert exc_info.value.code == 1


def test_validate_llm_keys_passes_with_only_groq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At-least-one provider check: GROQ alone is enough."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY_2", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "stub-groq")
    # Should not raise.
    tick_mod._validate_llm_keys_present()


def test_validate_llm_keys_passes_with_only_nvidia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At-least-one provider check: NVIDIA alone is enough."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY_2", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "stub-nvidia")
    # Should not raise.
    tick_mod._validate_llm_keys_present()


def test_validate_llm_keys_passes_with_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both NVIDIA + GROQ configured: no exit, no error. Uses
    monkeypatch (not direct ``os.environ`` mutation) so the env
    state is auto-restored after the test — preventing the leak
    that would otherwise bleed the stub keys into subsequent
    tests' env reads."""
    monkeypatch.setenv("NVIDIA_API_KEY", "stub-nvidia")
    monkeypatch.setenv("GROQ_API_KEY", "stub-groq")
    tick_mod._validate_llm_keys_present()


# ---------------------------------------------------------------------
# main() integration tests — exits 1 vs 0 based on env.
# ---------------------------------------------------------------------


def test_main_exits_1_when_database_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` with ``DATABASE_URL`` unset → exits 1 BEFORE any
    session_factory call (the script's pre-flight check)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "stub")
    # ``_parse_args()`` reads ``sys.argv`` for CLI args; pytest's
    # ``['tests/test_apply_worker_tick.py', '-v']`` would be
    # interpreted as unrecognized arguments and ``argparse`` would
    # call ``sys.exit(2)`` BEFORE our env-validation gets to fire.
    # Patch argv to a bare script-name so argparse falls back to its
    # defaults (``--timeout-seconds`` 3000).
    monkeypatch.setattr(sys, "argv", ["apply_worker_tick.py"])
    # ``main`` is async, so wrap in asyncio.run for sync testing.
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(tick_mod.main())
    assert exc_info.value.code == 1


def test_main_exits_1_when_no_llm_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` with both NVIDIA + GROQ unset → exits 1. Same
    gate the GHA workflow's secret-lint applies — the script's
    check is the final in-process validation that catches a
    misconfigured-but-not-empty secrets file."""
    monkeypatch.setenv("DATABASE_URL", _STUB_DATABASE_URL)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY_2", raising=False)
    monkeypatch.setattr(sys, "argv", ["apply_worker_tick.py"])
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(tick_mod.main())
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_main_exits_0_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` happy path: queue is empty → exits 0 with
    processed=0. Drives the whole ``main()`` body (env validation
    + LLM construction + drain loop) without real infra."""
    monkeypatch.setenv("DATABASE_URL", _STUB_DATABASE_URL)
    monkeypatch.setenv("GROQ_API_KEY", "stub")
    # See the sys.argv-rationale comment in the two tests above;
    # same fix here so argparse doesn't parse pytest's argv.
    monkeypatch.setattr(sys, "argv", ["apply_worker_tick.py"])

    # Patch _drain_loop to a no-op-async returning 0. We don't want
    # to also patch AsyncSessionLocal because the import chain
    # would be large; a single patch on _drain_loop is enough.
    async def fake_drain(_timeout_seconds: int) -> int:
        return 0

    monkeypatch.setattr(tick_mod, "_drain_loop", fake_drain)
    rc = await tick_mod.main()
    assert rc == 0
