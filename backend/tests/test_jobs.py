"""Tests for :mod:`routes.jobs` — exercises the wire shape the React
``JobsReview`` page + ``usePendingCount`` badge widget consume.

Patterns mirror :mod:`tests.test_pipeline`: in-memory seeded store,
``_seed()`` resets between tests.

The ``/api/jobs/pending-count`` endpoint is *not* reduced to a generic
``{{job_id}}`` lookup — that ordering is verified by :func:`TestPendingCount`
which asserts ``GET /api/jobs/pending-count`` returns ``2`` (= number of
``in_review`` records) although the URL *would* match a hypothetical
``GET /api/jobs/{job_id}`` route if one were added later.
"""
from __future__ import annotations

from datetime import datetime, timezone

import unittest

from fastapi.testclient import TestClient

from main import app
from routes.jobs import _JOBS_DB, _seed


class _JobsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _seed()
        self.client = TestClient(app)


# ---------------------------------------------------------------------------
class TestListAll(_JobsTestCase):
    def test_get_returns_every_seed_record(self) -> None:
        r = self.client.get("/api/jobs")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)
        ids = {j["id"] for j in body["jobs"]}
        self.assertEqual(
            ids,
            {"j_1", "j_2", "j_3", "j_4", "j_5", "j_6"},
        )

    def test_job_envelope_shape(self) -> None:
        body = self.client.get("/api/jobs").json()
        for j in body["jobs"]:
            self.assertIn("id", j)
            self.assertIn("status", j)
            self.assertIn("ats_type", j)
            self.assertIn("title", j)
            self.assertIn("company_name", j)
            self.assertIn("url", j)
            self.assertIn("ai_fit_score", j)
            self.assertIn("ai_fit_reasoning", j)
            self.assertIn("review_deadline", j)


# ---------------------------------------------------------------------------
class TestStatusFilter(_JobsTestCase):
    def test_filter_in_review_returns_two(self) -> None:
        r = self.client.get("/api/jobs?status=in_review")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        for j in body["jobs"]:
            self.assertEqual(j["status"], "in_review")

    def test_filter_unknown_status_returns_empty(self) -> None:
        r = self.client.get("/api/jobs?status=ghosted-by-them")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["jobs"], [])


# ---------------------------------------------------------------------------
class TestPageSize(_JobsTestCase):
    def test_page_size_caps_returned_count_but_total_still_full(self) -> None:
        r = self.client.get("/api/jobs?page_size=2")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # ``jobs`` is the page slice.
        self.assertEqual(len(body["jobs"]), 2)
        # ``total`` reflects the matched set *before* slicing so the
        # React list can render "showing 2 of 6".
        self.assertEqual(body["total"], 6)


# ---------------------------------------------------------------------------
class TestPendingCount(_JobsTestCase):
    def test_pending_count_matches_seeded_in_review_records(self) -> None:
        expected = sum(1 for j in _JOBS_DB.values() if j["status"] == "in_review")
        r = self.client.get("/api/jobs/pending-count")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["count"], expected)

    def test_pending_count_update_after_approve(self) -> None:
        before = self.client.get("/api/jobs/pending-count").json()["count"]
        self.client.post("/api/jobs/j_1/approve")
        after = self.client.get("/api/jobs/pending-count").json()["count"]
        self.assertEqual(after, before - 1)


# ---------------------------------------------------------------------------
class TestApprove(_JobsTestCase):
    def test_approve_flips_status_and_clears_deadline(self) -> None:
        # j_1 is in_review with a future deadline.
        before = _JOBS_DB["j_1"]
        self.assertEqual(before["status"], "in_review")
        self.assertIsNotNone(before["review_deadline"])
        # Verify the deadline parses and is in the future.
        deadline = datetime.fromisoformat(
            before["review_deadline"].replace("Z", "+00:00")
        )
        self.assertGreater(deadline, datetime.now(timezone.utc))

        r = self.client.post("/api/jobs/j_1/approve")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "approved")
        self.assertIsNone(body["review_deadline"])

    def test_approve_missing_returns_404(self) -> None:
        r = self.client.post("/api/jobs/does-not-exist/approve")
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------------
class TestReject(_JobsTestCase):
    def test_reject_flips_status_and_clears_deadline(self) -> None:
        r = self.client.post("/api/jobs/j_2/reject")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "rejected")
        self.assertIsNone(body["review_deadline"])

    def test_reject_missing_returns_404(self) -> None:
        r = self.client.post("/api/jobs/does-not-exist/reject")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
