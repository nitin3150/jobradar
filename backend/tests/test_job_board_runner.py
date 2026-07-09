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
import re
import unittest
from datetime import datetime, timedelta, timezone

from pipeline.nodes.jobs_boards.runner import (
    _build_relevant_patterns_from_roles,
    _display_name_for_slug,
)
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


class TestDisplayNameForSlug(unittest.TestCase):
    """Pin the org-slug → display-name mapping used by ``run_all``
    to populate the ``company_name`` field on each job.

    The board fetchers (ashby / greenhouse / lever) don't return a
    company name on each posting, so the runner has to derive one
    from the org slug. The override map handles brand names
    title-casing mangles (e.g. ``"openai"`` → ``"Openai"``), and
    the fallback is ``slug.replace("-", " ").title()``. Pinning
    both paths keeps a future "let's just use the slug" refactor
    from silently regressing the JobCard UI back to ugly
    "Stripe Inc" (was) vs "Stripe Inc" (correct) collisions.
    """

    def test_override_map_handles_brand_names(self) -> None:
        # These three are the cases where ``.title()`` produces
        # something an operator would call a regression. Pin them
        # so a future "drop the map" refactor breaks the test
        # rather than the UI.
        self.assertEqual(_display_name_for_slug("openai"), "OpenAI")
        self.assertEqual(_display_name_for_slug("xai"), "xAI")
        self.assertEqual(_display_name_for_slug("n8n"), "n8n")

    def test_override_map_handles_hyphenated_acronyms(self) -> None:
        # "Scale-AI" → "Scale Ai" via .title(); "Arize-AI" via
        # .title() — both look broken next to the brand's own
        # marketing spelling. Override catches both.
        self.assertEqual(_display_name_for_slug("scale-ai"), "Scale AI")
        self.assertEqual(_display_name_for_slug("arize-ai"), "Arize AI")

    def test_title_case_fallback_handles_hyphenated_slugs(self) -> None:
        # The common case: a hyphenated slug the operator hasn't
        # bothered to register in the override map. The title-case
        # fallback converts "stripe-inc" → "Stripe Inc" without
        # dragging in a hand-maintained map entry.
        self.assertEqual(_display_name_for_slug("stripe-inc"), "Stripe Inc")
        self.assertEqual(_display_name_for_slug("replicate"), "Replicate")
        self.assertEqual(_display_name_for_slug("mastra"), "Mastra")

    def test_unknown_slug_uses_title_case_fallback(self) -> None:
        # No override + no obvious tokenization — title() lower-cases
        # nothing, capitalises the first letter of each whitespace-
        # separated token, and leaves the rest alone. Pins the
        # contract: unknown slugs always render as something
        # readable, never as the literal slug.
        self.assertEqual(_display_name_for_slug("unknown-xyz"), "Unknown Xyz")
        self.assertEqual(_display_name_for_slug("deepmind"), "Deepmind")


class TestBuildRelevantPatternsFromRoles(unittest.TestCase):
    """Step-4: the boards runner's initial filter is now profile-aware.

    The runner calls :func:`_build_relevant_patterns_from_roles` on
    the operator's ``target_roles`` list and passes the result to
    :func:`utils.filters.filter_roles` as ``extra_relevant_patterns``.
    These tests pin the pattern-generation contract: every role
    becomes a strict substring match, special characters are
    regex-escaped, and the word-boundary semantics handle non-word
    characters at the edges (the "C++ Engineer" trap).
    """

    def test_plain_role_name_produces_strict_match(self) -> None:
        patterns = _build_relevant_patterns_from_roles(["AI Engineer"])
        self.assertEqual(len(patterns), 1)
        # Strict match: the pattern requires the exact phrase with
        # non-word characters on either side. "AI Engineer" appears
        # as a contiguous phrase, never as a partial overlap.
        self.assertTrue(
            re.search(patterns[0], "Senior AI Engineer"),
            "AI Engineer should match within Senior AI Engineer",
        )
        self.assertTrue(
            re.search(patterns[0], "AI Engineer at Replicate"),
            "AI Engineer should match at the start of a title",
        )

    def test_role_with_special_chars_is_regex_escaped(self) -> None:
        # "C++ Engineer" has regex metacharacters. Without
        # re.escape the "+" would match one-or-more of the
        # preceding character, producing a regex of its own.
        patterns = _build_relevant_patterns_from_roles(["C++ Engineer"])
        # If escaping is broken, the compile would raise
        # re.error (unbalanced parenthesis) or match wildly.
        compiled = re.compile(patterns[0])
        self.assertTrue(
            compiled.search("Senior C++ Engineer"),
            "C++ Engineer should match (escaping must preserve literal +)",
        )
        # And NOT match the unescaped interpretation: a "C" followed
        # by one-or-more of whatever character. "Ccc Engineer" is
        # not the same as "C++ Engineer" and must not match.
        self.assertFalse(
            compiled.search("Ccc Engineer"),
            "Escaping must treat + literally, not as a quantifier",
        )

    def test_role_with_slash_is_regex_escaped(self) -> None:
        # "AI/ML" has a regex metacharacter. The example profile
        # uses "AI/ML Engineer" as a primary archetype.
        patterns = _build_relevant_patterns_from_roles(["AI/ML Engineer"])
        compiled = re.compile(patterns[0])
        self.assertTrue(compiled.search("Senior AI/ML Engineer"))
        # The slash must be literal — "AI" alone should not match
        # because the pattern requires the slash.
        self.assertFalse(
            compiled.search("Senior AI Engineer"),
            "AI/ML pattern must not match titles without the slash",
        )

    def test_role_at_start_of_string_uses_lookbehind_not_word_boundary(self) -> None:
        # The "C++" trap: \bC\+\+ Engineer\b fails because
        # \b doesn't fire between + and a space (both are
        # non-word characters). The (?<!\w)...(?!\w) lookarounds
        # handle this correctly.
        patterns = _build_relevant_patterns_from_roles(["C++ Engineer"])
        compiled = re.compile(patterns[0])
        self.assertTrue(
            compiled.search("C++ Engineer at Acme"),
            "C++ Engineer at the start of a title must match",
        )

    def test_case_insensitive_match(self) -> None:
        # The (?i) flag is set so "ai engineer" in a job title
        # matches the "AI Engineer" pattern. Job titles arrive
        # in arbitrary case from the ATS fetchers.
        patterns = _build_relevant_patterns_from_roles(["AI Engineer"])
        compiled = re.compile(patterns[0])
        self.assertTrue(compiled.search("ai engineer"))
        self.assertTrue(compiled.search("AI ENGINEER"))
        self.assertTrue(compiled.search("Ai Engineer"))

    def test_partial_overlap_does_not_match(self) -> None:
        # Strict match — "AI" alone should not match the
        # "AI Engineer" pattern. The lookarounds assert the
        # surrounding characters are non-word, so a longer
        # word containing "AI" as a substring (e.g.
        # "AIDEN Engineer") also fails.
        patterns = _build_relevant_patterns_from_roles(["AI Engineer"])
        compiled = re.compile(patterns[0])
        self.assertFalse(
            compiled.search("AIDEN Engineer"),
            "Substring match within a longer word must not match",
        )

    def test_empty_list_returns_empty_list(self) -> None:
        # An empty target_roles list (operator cleared their
        # profile) yields an empty pattern list. The filter
        # treats this as a no-op and falls back to
        # DEFAULT_RELEVANT_PATTERNS.
        self.assertEqual(_build_relevant_patterns_from_roles([]), [])

    def test_whitespace_only_role_is_skipped(self) -> None:
        # A hand-edited profile.yml could sneak a blank entry
        # past the renderer. Don't generate a pattern for it.
        patterns = _build_relevant_patterns_from_roles(["", "  ", "AI Engineer"])
        self.assertEqual(len(patterns), 1)
        self.assertIn("AI Engineer", patterns[0])

    def test_whitespace_around_role_is_trimmed(self) -> None:
        # The renderer trims, but a hand-edited profile.yml
        # could leave a leading/trailing space. Trim defensively.
        patterns = _build_relevant_patterns_from_roles(["  AI Engineer  "])
        self.assertEqual(len(patterns), 1)
        compiled = re.compile(patterns[0])
        self.assertTrue(compiled.search("AI Engineer"))

    def test_multiple_roles_produce_multiple_patterns(self) -> None:
        # The example profile has 5 target roles. Each becomes
        # a separate pattern; the filter ORs them.
        patterns = _build_relevant_patterns_from_roles(
            [
                "Senior AI Engineer",
                "Staff ML Engineer",
                "AI/ML Engineer",
                "AI Product Manager",
                "Solutions Architect",
            ]
        )
        self.assertEqual(len(patterns), 5)
        # Every pattern compiles cleanly (no regex errors).
        for p in patterns:
            re.compile(p)


if __name__ == "__main__":
    unittest.main()
