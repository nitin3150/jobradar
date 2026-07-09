"""Boards-runner unit tests for the ``BOARDS_DELTA_HOURS`` env-var chain.

Written as ``unittest.TestCase`` (not bare pytest functions) so the
project's canonical runner ``python -m unittest discover tests -v``
(per ``backend/README.md``) can pick them up — pytest is intentionally
absent from ``pyproject.toml``. See ``runner.py::DEFAULT_DELTA_HOURS``
for the env-var source-of-truth.
"""
import importlib
import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone

from utils.time_check import parse_published_at


# Module-level handles so the helper methods stay short.
_ENV_VAR = "BOARDS_DELTA_HOURS"


class TestJobBoardRunner(unittest.TestCase):
    """Boards-runner env-var coverage + cutoff pinning."""

    def setUp(self) -> None:
        # Snapshot env state so the test's mutations don't bleed into
        # the next one (and so a host-leaked env doesn't poison the
        # counter-test's assertions).
        self._had_var = _ENV_VAR in os.environ
        self._saved_value = os.environ.get(_ENV_VAR)

    def tearDown(self) -> None:
        """Restore env + reload so module-level ``DEFAULT_DELTA_HOURS``
        re-evaluates against the saved state. Note: the reload rebinds
        module-level names (``ORG_INDEX``, ``DEFAULT_DELTA_HOURS``,
        board fetchers) to fresh objects — peer tests that
        ``==``-compare against ``ORG_INDEX`` are fine, but identity
        (``is``) assertions would flake across tests because every
        reload yields a new dict.
        """
        if self._had_var:
            os.environ[_ENV_VAR] = self._saved_value
        else:
            os.environ.pop(_ENV_VAR, None)
        from pipeline.nodes.jobs_boards import runner as runner_module
        importlib.reload(runner_module)

    def test_parse_published_at_handles_millisecond_timestamps(self) -> None:
        # 1772551058051 ms = 2026-03-03 15:17:38.051 UTC. The arguments
        # used to have a hand-typed expected datetime hard-coded; the
        # precise value changes whenever the host clock rolls a day,
        # so the test was always at risk of drifting. Write the
        # expected value by *computing* the same conversion here so
        # the assertion is stable against any future epoch drift.
        value_ms = 1772551058051
        parsed = parse_published_at(value_ms)
        expected = datetime.fromtimestamp(value_ms / 1000.0, tz=timezone.utc)
        # ``parse_published_at`` returns a tz-aware datetime; require
        # exact equality (microsecond precision).
        self.assertEqual(parsed, expected)

    def test_compute_since_cutoff_uses_the_latest_previous_scan(self) -> None:
        from pipeline.nodes.jobs_boards.runner import compute_since_cutoff

        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        last_run = now - timedelta(minutes=30)
        cutoff = compute_since_cutoff(
            now=now, delta_hours=1, last_run=last_run
        )
        self.assertEqual(cutoff, last_run)

    def test_BOARDS_DELTA_HOURS_env_var_defaults_to_168_when_unset(self) -> None:
        """Env unset → 168h default baseline (the lowest-risk counter-case)."""
        os.environ.pop(_ENV_VAR, None)
        from pipeline.nodes.jobs_boards import runner as runner_module

        importlib.reload(runner_module)
        self.assertEqual(runner_module.DEFAULT_DELTA_HOURS, 168)

    def test_BOARDS_DELTA_HOURS_env_var_overrides_default_delta_hours(self) -> None:
        """End-to-end override chain.

        ``BOARDS_DELTA_HOURS=24`` → ``DEFAULT_DELTA_HOURS=24``
        → ``run_all``'s positional default bound to 24 at def-time
        → ``compute_since_cutoff`` produces a ``now - 24h`` epoch, the
        value every ATS fetcher is ultimately handed.
        """
        os.environ[_ENV_VAR] = "24"
        from pipeline.nodes.jobs_boards import runner as runner_module

        importlib.reload(runner_module)

        # (a) module-level constant captured the override.
        self.assertEqual(runner_module.DEFAULT_DELTA_HOURS, 24)

        # (b) ``run_all``'s positional default bound at def-time.
        # ``inspect.signature(...)`` here relies on def-time binding:
        # ``importlib.reload`` re-executes the module body, which
        # re-binds the ``delta_hours=DEFAULT_DELTA_HOURS`` literal. If
        # a future refactor moves ``run_all``'s default to a
        # thread-local or lazy callable, this assertion silently starts
        # reading runtime values — keep the assertion next to the
        # comment so the contract is visible at edit-time.
        sig = inspect.signature(runner_module.run_all)
        self.assertEqual(sig.parameters["delta_hours"].default, 24)

        # (c) cutoff the runner hands to every ATS fetcher.
        now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
        cutoff = runner_module.compute_since_cutoff(
            now=now, delta_hours=runner_module.DEFAULT_DELTA_HOURS
        )
        self.assertEqual(cutoff, now - timedelta(hours=24))

    def test_BOARDS_DELTA_HOURS_rejects_malformed_value(self) -> None:
        """Non-integer env value (``BOARDS_DELTA_HOURS=foo``) → actionable
        ``SystemExit`` at import time, not a cryptic ``ValueError`` traceback.

        Locks in the operator-friendly boot-time contract: a typo in
        ``.env`` produces a single actionable error line instead of
        a Python stdlib traceback going to ``int()``.

        Also covers the empty-string variant (``BOARDS_DELTA_HOURS=``)
        which is a real operator gotcha when an ``.env`` file has a
        stray blank line for the variable — ``int("")`` raises the
        same ``ValueError`` and should hit the same ``SystemExit``
        path so the operator sees a single actionable line.
        """
        for bad_value in ("foo", ""):
            os.environ[_ENV_VAR] = bad_value
            from pipeline.nodes.jobs_boards import runner as runner_module

            with self.assertRaises(SystemExit) as cm:
                importlib.reload(runner_module)
            msg = str(cm.exception)
            # Match the stable substrings (narrower than exact phrase
            # so rewordings don't silently break the test, broader than
            # just one word so we still lock the operator-facing intent).
            self.assertIn("BOARDS_DELTA_HOURS", msg)
            self.assertIn("positive integer", msg)

    def test_BOARDS_DELTA_HOURS_rejects_non_positive_value(self) -> None:
        """Non-positive env value (``BOARDS_DELTA_HOURS=0`` or ``-5``) →
        actionable ``SystemExit``.

        A zero or negative lookback would make ``compute_since_cutoff``
        yield a *future* timestamp, defeating the per-org ``since``
        filter (every org would be queried for jobs published in the
        future → always-empty page → worker churn). The ``< 1`` floor
        turns that degenerate failure mode into a clear boot error.
        """
        os.environ[_ENV_VAR] = "0"
        from pipeline.nodes.jobs_boards import runner as runner_module

        with self.assertRaises(SystemExit) as cm:
            importlib.reload(runner_module)
        msg = str(cm.exception)
        self.assertIn("BOARDS_DELTA_HOURS", msg)
        self.assertIn("positive integer", msg)

        os.environ[_ENV_VAR] = "-5"
        with self.assertRaises(SystemExit) as cm:
            importlib.reload(runner_module)
        msg = str(cm.exception)
        self.assertIn("positive integer", msg)


if __name__ == "__main__":
    unittest.main()
