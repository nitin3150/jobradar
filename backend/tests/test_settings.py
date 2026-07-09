"""Tests for :mod:`routes.settings` — exercises the wire shape the
React ``PreferencesModal`` consumes (``usePreferences``).

The defaults in this test fixture mirror
:data:`frontend/src/hooks/usePreferences.DEFAULT_PREFERENCES` exactly
(roles list copy, `review_window_hours == 2`, `job_fit_threshold == 0.6`,
`send_followup_emails == True`); a drift in either side means the
initial paint before the GET resolves would render mismatched fields.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from main import app
from routes.settings import _reset_prefs


class _SettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Reset singleton via the public test seam so background PATCHes
        # from a previous case don't leak into this one.
        _reset_prefs()
        self.client = TestClient(app)


# Mirror frontend/src/hooks/usePreferences.DEFAULT_PREFERENCES verbatim.
# Post-merge cleanup: the legacy 4-role hardcoded list was removed
# from both the backend Pydantic default_factory and the frontend
# hook. The profile (config/profile.yml) is the source of truth
# for target roles now. The wire field stays in the schema for
# back-compat but is no longer read by any scoring code path.
_FRONTEND_DEFAULT_ROLES: list[str] = []


# ---------------------------------------------------------------------------
class TestGetDefaults(_SettingsTestCase):
    def test_get_returns_defaults_mirroring_frontend_hook(self) -> None:
        body = self.client.get("/api/settings").json()
        self.assertEqual(body["target_roles"], _FRONTEND_DEFAULT_ROLES)
        self.assertEqual(body["review_window_hours"], 2)
        self.assertEqual(body["job_fit_threshold"], 0.6)
        self.assertTrue(body["send_followup_emails"])


# ---------------------------------------------------------------------------
class TestPatchRoundTrip(_SettingsTestCase):
    def test_patch_review_window_hours_round_trips(self) -> None:
        r = self.client.patch(
            "/api/settings", json={"review_window_hours": 4},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["review_window_hours"], 4)
        # Untouched fields remain at their defaults.
        self.assertEqual(body["target_roles"], _FRONTEND_DEFAULT_ROLES)
        self.assertEqual(body["job_fit_threshold"], 0.6)
        self.assertTrue(body["send_followup_emails"])

    def test_patch_fit_threshold_round_trips(self) -> None:
        r = self.client.patch("/api/settings", json={"job_fit_threshold": 0.85})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertAlmostEqual(r.json()["job_fit_threshold"], 0.85)

    def test_patch_followup_toggle_round_trips(self) -> None:
        r = self.client.patch(
            "/api/settings", json={"send_followup_emails": False},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["send_followup_emails"])


# ---------------------------------------------------------------------------
class TestPatchNormalization(_SettingsTestCase):
    def test_patch_target_roles_trims_dedupes_drop_blanks(self) -> None:
        r = self.client.patch(
            "/api/settings",
            json={
                "target_roles": [
                    "  AI Engineer  ", "ML Engineer", "ai engineer",
                    "ML Engineer", "", "  ", "Backend",
                ]
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        # The normalization is *case-sensitive* dedupe — the lowercase
        # ``ai engineer`` collapses onto ``AI Engineer`` because we
        # trim but do NOT lowercase (matches the in-place usePreferences
        # semantics).
        self.assertEqual(
            r.json()["target_roles"],
            ["AI Engineer", "ML Engineer", "ai engineer", "Backend"],
        )


# ---------------------------------------------------------------------------
class TestPatchValidation(_SettingsTestCase):
    def test_patch_review_window_above_max_returns_422(self) -> None:
        r = self.client.patch(
            "/api/settings", json={"review_window_hours": 100},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_patch_fit_threshold_above_max_returns_422(self) -> None:
        r = self.client.patch(
            "/api/settings", json={"job_fit_threshold": 1.5},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_patch_fit_threshold_below_min_returns_422(self) -> None:
        r = self.client.patch(
            "/api/settings", json={"job_fit_threshold": -0.1},
        )
        self.assertEqual(r.status_code, 422, r.text)


# ---------------------------------------------------------------------------
class TestSeniorityDefaults(_SettingsTestCase):
    def test_defaults_include_null_min_and_max(self) -> None:
        body = self.client.get("/api/settings").json()
        self.assertIn("min_seniority", body)
        self.assertIn("max_seniority", body)
        self.assertIsNone(body["min_seniority"])
        self.assertIsNone(body["max_seniority"])


# ---------------------------------------------------------------------------
class TestSeniorityPatchRoundTrip(_SettingsTestCase):
    def test_patch_min_seniority_round_trips(self) -> None:
        r = self.client.patch("/api/settings", json={"min_seniority": "senior"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["min_seniority"], "senior")
        # Untouched fields remain at their defaults.
        self.assertIsNone(r.json()["max_seniority"])

    def test_patch_max_seniority_round_trips(self) -> None:
        r = self.client.patch("/api/settings", json={"max_seniority": "staff"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_seniority"], "staff")
        self.assertIsNone(r.json()["min_seniority"])

    def test_patch_both_bounds_round_trip(self) -> None:
        r = self.client.patch(
            "/api/settings",
            json={"min_seniority": "senior", "max_seniority": "staff"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["min_seniority"], "senior")
        self.assertEqual(body["max_seniority"], "staff")

    def test_patch_null_clears_bound(self) -> None:
        # Set then clear.
        self.client.patch("/api/settings", json={"min_seniority": "senior"})
        r = self.client.patch("/api/settings", json={"min_seniority": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["min_seniority"])


# ---------------------------------------------------------------------------
class TestSeniorityPatchValidation(_SettingsTestCase):
    def test_patch_min_seniority_unknown_value_returns_422(self) -> None:
        r = self.client.patch("/api/settings", json={"min_seniority": "phoenix"})
        self.assertEqual(r.status_code, 422, r.text)

    def test_patch_max_seniority_unknown_value_returns_422(self) -> None:
        r = self.client.patch("/api/settings", json={"max_seniority": "phoenix"})
        self.assertEqual(r.status_code, 422, r.text)

    def test_patch_min_exceeds_max_returns_422(self) -> None:
        r = self.client.patch(
            "/api/settings",
            json={"min_seniority": "vp", "max_seniority": "intern"},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_patch_min_equals_max_is_accepted(self) -> None:
        # Tight band (only this tier) is a degenerate but valid case.
        r = self.client.patch(
            "/api/settings",
            json={"min_seniority": "senior", "max_seniority": "senior"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_patch_only_one_bound_set_does_not_validate_cross(self) -> None:
        # PATCH ``min`` alone shouldn't trip a cross-bound error — the
        # operator may set ``min`` first and ``max`` later.
        r = self.client.patch("/api/settings", json={"min_seniority": "vp"})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
