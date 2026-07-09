"""Tests for :mod:`routes.jobs` — exercises the wire shape the React
``JobsReview`` page + ``usePendingCount`` badge widget consume.

Patterns mirror :mod:`tests.test_pipeline`: real Postgres-backed
store, ``_seed_job_rows()`` truncates + reseeds between tests so
mutations from one test don't leak into the next.

The ``/api/jobs/pending-count`` endpoint is *not* reduced to a generic
``{{job_id}}`` lookup — that ordering is verified by
:func:`TestPendingCount` which asserts ``GET /api/jobs/pending-count``
returns ``2`` (= number of ``in_review`` records) although the URL
*would* match a hypothetical ``GET /api/jobs/{job_id}`` route if one
were added later.

Each test seeds with ``_seed_job_rows(session)``, which both wipes
existing rows and installs the canonical 6 fixture rows, so tests
always see a clean state regardless of what scoring service writes
during a prior test run.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from db.session import AsyncSessionLocal
from main import app
from routes.jobs import _seed_job_rows, _seed_id_for


def _run(coro):
    """Helper to drive an async coroutine from a sync test body.

    Most tests stay synchronous (FastAPI's ``TestClient`` runs routes
    in a worker thread, so the test thread itself never enters an
    event loop). Use this for the setUp/tearDown hooks that need to
    truncate + reseed the `jobs` table or to peek at DB state.
    """
    return asyncio.run(coro)


class _JobsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _run(_seed_job_rows(_session()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        # Wipe the jobs table so a scoring-service write from this
        # test doesn't bleed into the next one (and so the test set
        # remains deterministic).
        from sqlalchemy import delete as sa_delete
        from db import models as db_models

        async def _wipe() -> None:
            async with AsyncSessionLocal() as session:
                await session.execute(sa_delete(db_models.Job))
                await session.commit()

        _run(_wipe())


def _session():
    """Helper to open a fresh AsyncSession — the test framework runs
    everything synchronously so we open + close around each setUp.
    """
    return AsyncSessionLocal()


# ---------------------------------------------------------------------
# Pre-generated UUID for the seeded ids so tests can address them by
# path string. Mirror of ``_seed_id_for`` in routes.jobs — keeping it
# here so test failures point at a stable, redacted-from-import URL.
# ---------------------------------------------------------------------
J_1_ID = str(_seed_id_for("j_1"))
J_2_ID = str(_seed_id_for("j_2"))
J_3_ID = str(_seed_id_for("j_3"))
J_4_ID = str(_seed_id_for("j_4"))
J_5_ID = str(_seed_id_for("j_5"))
J_6_ID = str(_seed_id_for("j_6"))


# ---------------------------------------------------------------------
class TestListAll(_JobsTestCase):
    def test_get_returns_every_seed_record(self) -> None:
        r = self.client.get("/api/jobs")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)
        ids = {j["id"] for j in body["jobs"]}
        self.assertEqual(
            ids,
            {J_1_ID, J_2_ID, J_3_ID, J_4_ID, J_5_ID, J_6_ID},
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


# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
class TestPendingCount(_JobsTestCase):
    def test_pending_count_matches_seeded_in_review_records(self) -> None:
        r = self.client.get("/api/jobs/pending-count")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["count"], 2)

    def test_pending_count_update_after_approve(self) -> None:
        before = self.client.get("/api/jobs/pending-count").json()["count"]
        self.client.post(f"/api/jobs/{J_1_ID}/approve")
        after = self.client.get("/api/jobs/pending-count").json()["count"]
        self.assertEqual(after, before - 1)


# ---------------------------------------------------------------------
class TestApprove(_JobsTestCase):
    def test_approve_flips_status_and_clears_deadline(self) -> None:
        # Verify the seeded j_1 has a future deadline before approve.
        before = self.client.get(f"/api/jobs?status=in_review").json()["jobs"]
        j1 = next(j for j in before if j["id"] == J_1_ID)
        self.assertEqual(j1["status"], "in_review")
        self.assertIsNotNone(j1["review_deadline"])
        deadline = datetime.fromisoformat(
            j1["review_deadline"].replace("Z", "+00:00")
        )
        self.assertGreater(deadline, datetime.now(timezone.utc))

        r = self.client.post(f"/api/jobs/{J_1_ID}/approve")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], J_1_ID)
        self.assertEqual(body["status"], "approved")
        self.assertIsNone(body["review_deadline"])

    def test_approve_missing_returns_404(self) -> None:
        # Use a syntactically-valid UUID that doesn't exist.
        r = self.client.post("/api/jobs/00000000-0000-0000-0000-000000000000/approve")
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------
class TestReject(_JobsTestCase):
    def test_reject_flips_status_and_clears_deadline(self) -> None:
        r = self.client.post(f"/api/jobs/{J_2_ID}/reject")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], J_2_ID)
        self.assertEqual(body["status"], "rejected")
        self.assertIsNone(body["review_deadline"])

    def test_reject_missing_returns_404(self) -> None:
        r = self.client.post("/api/jobs/00000000-0000-0000-0000-000000000000/reject")
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------
class TestBadUuid(_JobsTestCase):
    def test_approve_with_non_uuid_string_returns_404(self) -> None:
        # The route treats malformed-UUID input as "not found" rather
        # than 422 because the "did this row exist?" check is the
        # primary failure mode we want to surface in the operator log.
        r = self.client.post("/api/jobs/not-a-uuid/approve")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
