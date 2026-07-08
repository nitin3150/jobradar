"""Tests for :mod:`routes.applications` — exercises the wire shape the
React ``ApplicationTracker`` consumes.

Mirrors the pattern from :mod:`tests.test_companies`: in-memory seeded
store, ``_seed()`` resets between tests via deep-copy.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from main import app
from routes.applications import _APPLICATIONS_DB, _seed


class _ApplicationsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _seed()
        self.client = TestClient(app)


# ---------------------------------------------------------------------------
class TestList(_ApplicationsTestCase):
    def test_get_returns_every_seed_record(self) -> None:
        r = self.client.get("/api/applications")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)
        ids = {a["id"] for a in body["applications"]}
        self.assertEqual(ids, {"a_1", "a_2", "a_3", "a_4", "a_5", "a_6"})

    def test_envelope_shape(self) -> None:
        body = self.client.get("/api/applications").json()
        for app in body["applications"]:
            self.assertIn("id", app)
            self.assertIn("job_title", app)
            self.assertIn("company_name", app)
            self.assertIn("submitted_at", app)
            self.assertIn("status", app)
            self.assertIn("last_email_at", app)
            self.assertIn("submission_screenshot_path", app)
            self.assertIn("notes", app)

    def test_status_filter_returns_only_matching(self) -> None:
        r = self.client.get("/api/applications?status=interview")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        for app in body["applications"]:
            self.assertEqual(app["status"], "interview")

    def test_status_filter_unknown_returns_empty(self) -> None:
        body = self.client.get("/api/applications?status=offrd-by-them").json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["applications"], [])

    def test_list_sorted_by_submitted_at_desc(self) -> None:
        body = self.client.get("/api/applications").json()
        timestamps = [a["submitted_at"] for a in body["applications"]]
        # Newest first; ISO 8601 lex sort equals chronological sort
        # because all timestamps end in ``Z`` (UTC) at second precision.
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        # The seeded ``a_6`` was submitted 1 day ago, the most recent.
        self.assertEqual(body["applications"][0]["id"], "a_6")
        # Full id-order pin: a future seed mutation that reorders or
        # adds a row surfaces here, before the React UI silently shifts.
        self.assertEqual(
            [a["id"] for a in body["applications"]],
            ["a_6", "a_1", "a_2", "a_5", "a_3", "a_4"],
        )


# ---------------------------------------------------------------------------
class TestPageSize(_ApplicationsTestCase):
    def test_page_size_caps_count_but_total_reflects_full_match(self) -> None:
        body = self.client.get("/api/applications?page_size=2").json()
        self.assertEqual(len(body["applications"]), 2)
        self.assertEqual(body["total"], 6)


# ---------------------------------------------------------------------------
class TestPatchStatus(_ApplicationsTestCase):
    def test_patch_flips_status(self) -> None:
        r = self.client.patch(
            "/api/applications/a_1/status",
            json={"status": "interview"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], "a_1")
        self.assertEqual(body["status"], "interview")

    def test_patch_appends_notes(self) -> None:
        r = self.client.patch(
            "/api/applications/a_1/status",
            json={"status": "submitted", "notes": "Recruiter call notes go here."},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["notes"], "Recruiter call notes go here.")

    def test_patch_omitting_notes_leaves_them_untouched(self) -> None:
        # Seeded a_2 already has notes; a PATCH without notes should not wipe them.
        original_notes = _APPLICATIONS_DB["a_2"]["notes"]
        r = self.client.patch(
            "/api/applications/a_2/status", json={"status": "ghosted"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["notes"], original_notes)

    def test_patch_missing_returns_404(self) -> None:
        r = self.client.patch(
            "/api/applications/does-not-exist/status",
            json={"status": "rejected"},
        )
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------------
class TestStatusValidation(_ApplicationsTestCase):
    def test_patch_with_unknown_status_returns_422(self) -> None:
        r = self.client.patch(
            "/api/applications/a_1/status",
            json={"status": "maybe-yes"},
        )
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main()
