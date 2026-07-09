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


# ---------------------------------------------------------------------
# v0.5 additions: PATCH /api/jobs/{id}/status writes a job_status_history
# row in the SAME transaction as the jobs.status update, and POST
# /api/jobs/{id}/research hits the LLMClient with the job payload
# (mocked here) and persists a research_reports row.
#
# The LLMClient is patched via ``unittest.mock.patch`` because we
# don't want test runs to hit NVIDIA / Groq; the FastAPI app's
# POST /research calls ``LLMClient.from_env()`` synchronously and
# awaits ``research_opportunity``. The mock returns a fixed Markdown
# string so the response shape is deterministic.
# ---------------------------------------------------------------------
class TestPatchJobStatus(_JobsTestCase):
    """Canonical status writer. Updates ``jobs.status`` AND inserts a
    ``job_status_history`` row in the same transaction so a future
    audit query can never observe a status change with no history.
    """

    def test_patch_to_approved_writes_history_row(self) -> None:
        from db import models as db_models
        from sqlalchemy import select

        # Snapshot the in_review row's id so we can verify the
        # history row's job_id matches.
        j1_id = _seed_id_for("j_1")

        # Pre-state: j_1 is in_review with a future deadline.
        before = self.client.get("/api/jobs?status=in_review").json()["jobs"]
        self.assertTrue(any(j["id"] == str(j1_id) for j in before))

        # PATCH j_1 → approved.
        r = self.client.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "approved", "source": "user", "note": "looks great"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], str(j1_id))
        self.assertEqual(body["status"], "approved")
        # ``approved`` is a terminal status → the review_deadline
        # should be cleared by the patch.
        self.assertIsNone(body["review_deadline"])

        # History row: ONE row, from_status='in_review',
        # to_status='approved', source='user', note='looks great'.
        async def _fetch_history() -> list[db_models.JobStatusHistory]:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.JobStatusHistory)
                    .where(db_models.JobStatusHistory.job_id == j1_id)
                    .order_by(db_models.JobStatusHistory.changed_at.asc())
                )
                return list((await session.execute(stmt)).scalars().all())

        history = _run(_fetch_history())
        self.assertEqual(len(history), 1)
        h = history[0]
        self.assertEqual(h.from_status, "in_review")
        self.assertEqual(h.to_status, "approved")
        self.assertEqual(h.source, "user")
        self.assertEqual(h.note, "looks great")
        # ``changed_at`` is auto-stamped by the DB default.
        self.assertIsNotNone(h.changed_at)

    def test_patch_unknown_job_returns_404(self) -> None:
        r = self.client.patch(
            "/api/jobs/00000000-0000-0000-0000-000000000000/status",
            json={"status": "approved"},
        )
        self.assertEqual(r.status_code, 404, r.text)

    def test_patch_invalid_status_value_returns_422(self) -> None:
        # Pydantic validates the Literal at the route's request-body
        # parsing step; an unknown status is a 422, not a 400.
        j1_id = _seed_id_for("j_1")
        r = self.client.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "ghosted-by-them"},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_patch_back_to_in_review_does_not_set_new_deadline(self) -> None:
        # PATCH-ing an approved job back to in_review must NOT crash
        # and must not invent a deadline — the deadline was cleared
        # by the prior approve, and the route does not re-seed one
        # when transitioning back to a non-terminal status.
        j1_id = _seed_id_for("j_1")
        self.client.post(f"/api/jobs/{j1_id}/approve")
        r = self.client.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "in_review"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "in_review")
        self.assertIsNone(r.json()["review_deadline"])

    def test_patch_default_source_is_user(self) -> None:
        # No source field → defaults to ``user`` per the
        # ``JobStatusPatch`` Pydantic model. An operator-click is
        # the audit-trail default; only programmatic changes opt
        # into ``auto_apply`` or similar.
        from db import models as db_models
        from sqlalchemy import select

        j1_id = _seed_id_for("j_1")
        self.client.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "rejected"},
        )
        async def _fetch_history() -> db_models.JobStatusHistory:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.JobStatusHistory)
                    .where(db_models.JobStatusHistory.job_id == j1_id)
                )
                return (await session.execute(stmt)).scalars().first()
        h = _run(_fetch_history())
        self.assertEqual(h.source, "user")


# ---------------------------------------------------------------------
class TestPostResearch(_JobsTestCase):
    """Sync Interview Prep. The route calls ``LLMClient.from_env()``
    which is patched here to return a fixed Markdown brief so the
    test is deterministic and offline.
    """

    def _mock_llm_research(self, content: str = "## Company Snapshot\nstub brief", model: str = "meta/llama-3.1-70b-instruct"):
        """Patch :class:`LLMClient.from_env` and the per-instance
        ``research_opportunity`` to return a fixed tuple.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        # We need both from_env (class method) AND the research_opportunity
        # coroutine. Patch the class method on the LLMClient symbol that
        # ``routes.jobs`` imports.
        mock_client = MagicMock()
        mock_client.research_opportunity = AsyncMock(return_value=(content, model))
        return patch("routes.jobs.LLMClient.from_env", return_value=mock_client)

    def test_research_happy_path_persists_report(self) -> None:
        from db import models as db_models
        from sqlalchemy import select

        j1_id = _seed_id_for("j_1")
        with self._mock_llm_research("## Company Snapshot\nreal content", "test-model"):
            r = self.client.post(f"/api/jobs/{j1_id}/research")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["job_id"], str(j1_id))
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["content"], "## Company Snapshot\nreal content")
        self.assertEqual(body["model_used"], "test-model")
        self.assertIsNone(body["error"])
        self.assertIsNotNone(body["requested_at"])
        self.assertIsNotNone(body["generated_at"])

        # DB assertion: the research_reports row exists with the
        # right job_id and content.
        async def _fetch_report() -> db_models.ResearchReport:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.ResearchReport)
                    .where(db_models.ResearchReport.job_id == j1_id)
                )
                return (await session.execute(stmt)).scalars().first()
        report = _run(_fetch_report())
        self.assertIsNotNone(report)
        self.assertEqual(report.status, db_models.RESEARCH_STATUS_READY)
        self.assertEqual(report.content, "## Company Snapshot\nreal content")
        self.assertEqual(report.model_used, "test-model")
        self.assertIsNone(report.error)

    def test_research_unknown_job_returns_404(self) -> None:
        r = self.client.post(
            "/api/jobs/00000000-0000-0000-0000-000000000000/research"
        )
        self.assertEqual(r.status_code, 404, r.text)

    def test_research_llm_failure_persists_failed_row_and_returns_502(self) -> None:
        # When every LLM provider fails, ``research_opportunity``
        # raises ``RuntimeError``. The route catches it, persists a
        # ``status='failed'`` research_reports row, and returns 502
        # so the React modal can surface the error verbatim.
        from unittest.mock import AsyncMock, MagicMock, patch
        from db import models as db_models
        from sqlalchemy import select

        j1_id = _seed_id_for("j_1")
        mock_client = MagicMock()
        mock_client.research_opportunity = AsyncMock(
            side_effect=RuntimeError("all LLM providers failed; last error type=APIConnectionError")
        )
        with patch("routes.jobs.LLMClient.from_env", return_value=mock_client):
            r = self.client.post(f"/api/jobs/{j1_id}/research")
        self.assertEqual(r.status_code, 502, r.text)
        # The 502 detail carries the operator-visible error.
        self.assertIn("research failed", r.json()["detail"].lower())

        # The research_reports row is still persisted so the
        # operator can see what happened in the JobBoard modal's
        # later reload.
        async def _fetch_reports() -> list[db_models.ResearchReport]:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.ResearchReport)
                    .where(db_models.ResearchReport.job_id == j1_id)
                )
                return list((await session.execute(stmt)).scalars().all())
        reports = _run(_fetch_reports())
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.status, db_models.RESEARCH_STATUS_FAILED)
        self.assertIsNone(rep.content)
        self.assertIsNone(rep.model_used)
        self.assertIn("APIConnectionError", rep.error or "")

    def test_research_no_api_keys_persists_failed_row(self) -> None:
        # ``LLMClient.from_env()`` itself raises ``RuntimeError``
        # when no API key is configured. The route catches that
        # the same way as a per-call LLM failure.
        from db import models as db_models
        from sqlalchemy import select
        from unittest.mock import patch

        j1_id = _seed_id_for("j_1")
        with patch(
            "routes.jobs.LLMClient.from_env",
            side_effect=RuntimeError("no LLM provider configured"),
        ):
            r = self.client.post(f"/api/jobs/{j1_id}/research")
        self.assertEqual(r.status_code, 502, r.text)

        async def _fetch_reports() -> list[db_models.ResearchReport]:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.ResearchReport)
                    .where(db_models.ResearchReport.job_id == j1_id)
                )
                return list((await session.execute(stmt)).scalars().all())
        reports = _run(_fetch_reports())
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].status, db_models.RESEARCH_STATUS_FAILED)
        self.assertIn("no LLM provider", reports[0].error or "")


# ---------------------------------------------------------------------
class TestGetResearch(_JobsTestCase):
    """GET /api/jobs/{id}/research re-opens the most recent ready
    report without a fresh LLM call. Used by the React modal's
    reload path.
    """

    def test_get_research_with_no_reports_returns_404(self) -> None:
        j1_id = _seed_id_for("j_1")
        r = self.client.get(f"/api/jobs/{j1_id}/research")
        self.assertEqual(r.status_code, 404, r.text)

    def test_get_research_returns_latest_ready_report(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch
        j1_id = _seed_id_for("j_1")
        # Stub two research calls — only the latest ready one is
        # returned.
        mock_client = MagicMock()
        mock_client.research_opportunity = AsyncMock(return_value=("brief v2", "test-model"))
        with patch("routes.jobs.LLMClient.from_env", return_value=mock_client):
            r1 = self.client.post(f"/api/jobs/{j1_id}/research")
            self.assertEqual(r1.status_code, 200, r1.text)
        # Second call should hit the cache.
        with patch("routes.jobs.LLMClient.from_env", return_value=mock_client):
            r2 = self.client.post(f"/api/jobs/{j1_id}/research")
            self.assertEqual(r2.status_code, 200, r2.text)
        # GET returns the most recent.
        r3 = self.client.get(f"/api/jobs/{j1_id}/research")
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(r3.json()["content"], "brief v2")


# ---------------------------------------------------------------------
class TestBadUuid(_JobsTestCase):
    def test_approve_with_non_uuid_string_returns_404(self) -> None:
        # The route treats malformed-UUID input as "not found" rather
        # than 422 because the "did this row exist?" check is the
        # primary failure mode we want to surface in the operator log.
        r = self.client.post("/api/jobs/not-a-uuid/approve")
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------
# v0.5 delivery: multi-status filter (?status=in_review,approved) +
# score range (score_min + score_max) so the React JobBoard can
# filter on a single slider without a separate dropdown.
# ---------------------------------------------------------------------
class TestMultiStatusFilter(_JobsTestCase):
    """Comma-separated ?status= query param.

    The seed has 2x in_review, 1x approved, 1x rejected, 1x applied,
    1x flagged. The multi-status OR query should return the union of
    the requested sets, and the wire ``total`` should reflect the
    matched count *before* the page-size slice.
    """

    def test_two_statuses_or_returns_union(self) -> None:
        r = self.client.get("/api/jobs?status=in_review,approved")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # 2 in_review + 1 approved = 3
        self.assertEqual(body["total"], 3)
        seen = {j["status"] for j in body["jobs"]}
        self.assertTrue(seen.issubset({"in_review", "approved"}))
        self.assertGreater(len(seen), 0)  # at least one of each

    def test_three_statuses_or_returns_union(self) -> None:
        r = self.client.get("/api/jobs?status=in_review,approved,rejected")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 4)

    def test_all_five_statuses_returns_full_set(self) -> None:
        r = self.client.get(
            "/api/jobs?status=in_review,approved,rejected,applied,flagged"
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 6)

    def test_unknown_in_list_short_circuits_to_empty(self) -> None:
        # An unknown fragment anywhere in the comma-separated list
        # short-circuits the whole query — same contract as the
        # single-status path, so a typo at the call site can't
        # reach the SQLAlchemy ``status = ANY(...)`` with a bad
        # enum value.
        r = self.client.get("/api/jobs?status=in_review,ghosted-by-them")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 0)
        self.assertEqual(r.json()["jobs"], [])

    def test_single_status_still_works(self) -> None:
        # Backward-compat: ?status=in_review (no comma) must still
        # return the in_review rows. The new path branches to the
        # old ``WHERE status = $1`` form for the single-value case
        # to keep the SQL plan cheap.
        r = self.client.get("/api/jobs?status=in_review")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 2)
        for j in r.json()["jobs"]:
            self.assertEqual(j["status"], "in_review")


# ---------------------------------------------------------------------
class TestScoreRangeFilter(_JobsTestCase):
    """score_min + score_max together form a half-open / closed range.

    Seed: scores are 0.86, 0.78, 0.91, 0.42, 0.74, 0.58. The default
    (0.0, 1.0) returns all 6. Tightening either bound drops rows.
    """

    def test_default_range_returns_all(self) -> None:
        r = self.client.get("/api/jobs")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 6)

    def test_score_min_floor_drops_below_threshold(self) -> None:
        # >= 0.8 → keeps 0.86, 0.91 → 2 rows
        r = self.client.get("/api/jobs?score_min=0.8")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        for j in body["jobs"]:
            self.assertGreaterEqual(j["ai_fit_score"], 0.8)

    def test_score_max_ceiling_drops_above_threshold(self) -> None:
        # <= 0.5 → keeps 0.42 → 1 row
        r = self.client.get("/api/jobs?score_max=0.5")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertLessEqual(body["jobs"][0]["ai_fit_score"], 0.5)

    def test_score_min_and_max_together(self) -> None:
        # 0.5 <= score <= 0.8 → 0.78, 0.74, 0.58 → 3 rows
        r = self.client.get("/api/jobs?score_min=0.5&score_max=0.8")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 3)
        for j in body["jobs"]:
            self.assertGreaterEqual(j["ai_fit_score"], 0.5)
            self.assertLessEqual(j["ai_fit_score"], 0.8)

    def test_score_min_out_of_range_returns_422(self) -> None:
        # Pydantic / FastAPI's ``Query(ge=0.0, le=1.0)`` rejects
        # out-of-range values with 422 before the route body runs.
        r = self.client.get("/api/jobs?score_min=1.5")
        self.assertEqual(r.status_code, 422, r.text)

    def test_score_max_out_of_range_returns_422(self) -> None:
        r = self.client.get("/api/jobs?score_max=-0.1")
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main()
