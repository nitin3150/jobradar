"""Tests for :mod:`scripts.boards_scan` — the 3-tier profile resolution.

Post-merge cleanup dropped the hardcoded 4-role ``DEFAULT_TARGET_ROLES``
fallback. The 3-tier resolution in :func:`scripts.boards_scan._resolve_profile`
is now: ``--target-roles`` CLI override (non-empty) → ``config/profile.yml``
(or example fallback) → ``TARGET_ROLES`` env var → render
``"(no profile configured)"`` and let the 7-factor LLM SYSTEM_PROMPT
degrade gracefully.

These tests pin that contract so a future "let me just add the
4-role default back" refactor breaks the test rather than the
production scoring path.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path as _RealPath

from services import profile_service


class _MissingPath:
    """Minimal Path-like stand-in for the monkeypatch — avoids
    pulling in :class:`pathlib.Path` semantics just to fake a
    missing file.

    :meth:`is_file` returns ``False`` so :func:`get_profile_path`
    and :func:`load_profile` both treat the path as missing.
    The class also stringifies to a recognisable fake path so
    log lines (e.g. ``loaded profile from {PROFILE_PATH}``)
    are obvious in test output if a future assertion goes red.
    """

    def __init__(self, raw: str = "/nonexistent/profile.yml") -> None:
        self._raw = raw

    def is_file(self) -> bool:  # noqa: D401 — pathlib API shim
        return False

    def __str__(self) -> str:
        return self._raw


class TestResolveProfile(unittest.TestCase):
    """Pin the 3-tier resolution contract in :func:`_resolve_profile`.

    Each test patches ``profile_service.PROFILE_PATH`` and
    ``profile_service.EXAMPLE_PATH`` to point at a ``_MissingPath``
    shim so :func:`profile_service.load_profile` returns an
    empty :class:`Profile` regardless of which fallback the
    loader would otherwise consult. Without the EXAMPLE_PATH
    override, the committed ``config/profile.example.yml``
    (5 target roles) would be loaded and the "empty profile"
    test paths would silently pass via the example data —
    masking regressions where the new 3-tier resolution
    stops working.

    The originals are snapshotted in :meth:`setUp` and restored
    in :meth:`tearDown` so the monkeypatching doesn't leak into
    sibling test files in the same process (the boards runner's
    profile paths are module-level globals, not pytest fixtures).
    """

    def setUp(self) -> None:
        # Snapshot the real path objects so tearDown can restore.
        # ``profile_service.PROFILE_PATH`` is a ``pathlib.Path`` at
        # import time — we keep references to those exact objects
        # rather than re-constructing from strings (which would
        # re-trigger any :mod:`pathlib` quirks on Windows paths
        # the runner never sees in CI).
        self._real_profile_path = profile_service.PROFILE_PATH
        self._real_example_path = profile_service.EXAMPLE_PATH
        profile_service.PROFILE_PATH = _MissingPath()
        profile_service.EXAMPLE_PATH = _MissingPath()
        profile_service.reset_cache()

        # Snapshot env state so a host-leaked ``TARGET_ROLES``
        # doesn't poison the "env is unset" assertions.
        self._had_target_roles = "TARGET_ROLES" in os.environ
        self._saved_target_roles = os.environ.get("TARGET_ROLES")

    def tearDown(self) -> None:
        # Restore the real paths FIRST so a tearDown failure
        # doesn't leave the module in a wiped state for the
        # next test class.
        profile_service.PROFILE_PATH = self._real_profile_path
        profile_service.EXAMPLE_PATH = self._real_example_path
        profile_service.reset_cache()

        if self._had_target_roles:
            os.environ["TARGET_ROLES"] = self._saved_target_roles
        else:
            os.environ.pop("TARGET_ROLES", None)

    def test_cli_override_wins_over_profile_and_env(self) -> None:
        # Profile empty + env unset + CLI override — the override
        # must take precedence and render the override roles as
        # the only primary target roles. Archetypes are dropped
        # (the override REPLACES the profile's target_roles
        # entirely) so the rendered prompt lists only ``X`` /
        # ``Y``.
        os.environ.pop("TARGET_ROLES", None)
        from scripts.boards_scan import _resolve_profile

        result = _resolve_profile(["X", "Y"])
        self.assertIn("Primary (dream roles):", result)
        self.assertIn("- X", result)
        self.assertIn("- Y", result)
        # No fallback messages — the override path was taken.
        self.assertNotIn("(no profile configured)", result)

    def test_env_var_used_when_profile_empty(self) -> None:
        # Profile is empty, env has roles, no override — env wins.
        os.environ["TARGET_ROLES"] = "Engineer,ML Engineer,Backend"
        from scripts.boards_scan import _resolve_profile

        result = _resolve_profile(None)
        self.assertIn("Engineer", result)
        self.assertIn("ML Engineer", result)
        self.assertIn("Backend", result)
        # Profile is empty so the env path was taken — the
        # profile-driven archetypes are NOT rendered.
        self.assertNotIn("Senior AI Engineer", result)

    def test_empty_profile_and_unset_env_renders_sentinel(self) -> None:
        # The NEW post-cleanup path: profile empty, env unset, no
        # override. The script must NOT fall back to a hardcoded
        # list — the LLM prompt must render the sentinel string
        # ``"(no profile configured)"`` so the 7-factor SYSTEM_PROMPT
        # can degrade gracefully.
        os.environ.pop("TARGET_ROLES", None)
        from scripts.boards_scan import _resolve_profile

        result = _resolve_profile(None)
        # Empty profile + no env = sentinel string the LLM
        # understands as "no candidate context, score on the
        # job alone with the remaining 7-factor clauses".
        self.assertEqual(result, "(no profile configured)")

    def test_empty_list_override_treated_as_no_override(self) -> None:
        # An empty list ``--target-roles=""`` is treated as
        # "operator didn't actually override" (per the
        # _resolve_profile docstring). This test pins that
        # contract: with profile empty + env unset + empty
        # override, the sentinel path runs (NOT a destructive
        # "override with zero roles").
        os.environ.pop("TARGET_ROLES", None)
        from scripts.boards_scan import _resolve_profile

        result = _resolve_profile([])
        # Empty override -> falls through to the empty-profile
        # branch -> renders the sentinel.
        self.assertEqual(result, "(no profile configured)")

    def test_no_hardcoded_four_role_fallback(self) -> None:
        # Regression guard for the 4th-tier removal: a profile
        # empty + env empty + no override must NOT silently
        # inject the legacy 4-role hardcoded list. The 4 roles
        # were ``["AI Engineer", "Machine Learning Engineer",
        # "LLM Engineer", "Software Engineer"]`` — none of those
        # strings should appear in the sentinel rendering.
        os.environ.pop("TARGET_ROLES", None)
        from scripts.boards_scan import _resolve_profile

        result = _resolve_profile(None)
        for legacy_role in (
            "AI Engineer",
            "Machine Learning Engineer",
            "LLM Engineer",
            "Software Engineer",
        ):
            self.assertNotIn(
                legacy_role,
                result,
                f"legacy role {legacy_role!r} leaked into the "
                f"empty-profile rendering — the hardcoded 4-role "
                f"fallback was supposed to be removed",
            )

    def test_profile_roles_used_when_no_override(self) -> None:
        # Profile has roles, env unset, no override — profile wins.
        # We restore the real PROFILE_PATH/EXAMPLE_PATH for this
        # one test so the example profile's 5 target roles are
        # visible to the resolver. The originals are restored in
        # tearDown regardless of which test ran first.
        #
        # ``_RealPath(...)`` (not ``profile_service.Path``) — the
        # former is the explicit ``pathlib.Path`` import at the
        # top of this file; the latter reaches into
        # ``profile_service``'s import surface for a name that
        # could be renamed by a future refactor (``from pathlib
        # import Path as _Path`` would silently break the test).
        # Using a local alias costs one line and makes the
        # dependency on ``pathlib`` explicit.
        profile_service.PROFILE_PATH = _RealPath(
            str(self._real_profile_path)
        )
        profile_service.EXAMPLE_PATH = _RealPath(
            str(self._real_example_path)
        )
        profile_service.reset_cache()
        os.environ.pop("TARGET_ROLES", None)
        from scripts.boards_scan import _resolve_profile

        result = _resolve_profile(None)
        self.assertIn("Senior AI Engineer", result)
        self.assertNotIn("(no profile configured)", result)


if __name__ == "__main__":
    unittest.main()
