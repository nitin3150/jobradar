"""Tests for :mod:`routes.jobs` — exercises the wire shape the React
``JobsReview`` page + ``usePendingCount`` badge widget consume.

Pattern: real Postgres-backed store. Each test takes the
``seeded_jobs`` async fixture (see :mod:`conftest`) which
truncates + reseeds the ``jobs`` table to the canonical 6 fixture
rows and yields an in-process ``httpx.AsyncClient`` wired to the
FastAPI app.

Why async + httpx.AsyncClient (was unittest.TestCase + TestClient):
the previous ``setUp`` called ``asyncio.run(_seed_job_rows(...))``
to populate the DB from a sync test body. With pytest-asyncio
installed, the session loop is alive in the test thread, so
``asyncio.run()`` raises ``RuntimeError: cannot be called from a
running event loop``. Async fixtures + async test methods keep
every coroutine on the same loop the runner provides — see
``pyproject.toml``'s ``[tool.pytest.ini_options]`` for the
``asyncio_mode = "auto"`` setting that lets ``async def test_*``
+ ``@pytest_asyncio.fixture`` be recognized without per-test
``@pytest.mark.asyncio`` decoration.

The ``/api/jobs/pending-count`` endpoint is *not* reduced to a
generic ``{{job_id}}`` lookup — that ordering is verified by
:func:`TestPendingCount` which asserts ``GET /api/jobs/pending-count``
returns ``2`` (= number of ``in_review`` records) although the URL
*would* match a hypothetical ``GET /api/jobs/{job_id}`` route if
one were added later.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from db import models as db_models
from db.session import AsyncSessionLocal
from routes.jobs import _seed_id_for


# Pre-generated UUID for the seeded ids so tests can address them by
# path string. Mirror of ``_seed_id_for`` in routes.jobs — keeping it
# here so test failures point at a stable, redacted-from-import URL.
J_1_ID = str(_seed_id_for("j_1"))
J_2_ID = str(_seed_id_for("j_2"))
J_3_ID = str(_seed_id_for("j_3"))
J_4_ID = str(_seed_id_for("j_4"))
J_5_ID = str(_seed_id_for("j_5"))
J_6_ID = str(_seed_id_for("j_6"))


# ---------------------------------------------------------------------
class TestListAll:
    async def test_get_returns_every_seed_record(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 6
        ids = {j["id"] for j in body["jobs"]}
        assert ids == {J_1_ID, J_2_ID, J_3_ID, J_4_ID, J_5_ID, J_6_ID}

    async def test_job_envelope_shape(self, seeded_jobs: AsyncClient) -> None:
        body = (await seeded_jobs.get("/api/jobs")).json()
        for j in body["jobs"]:
            assert "id" in j
            assert "status" in j
            assert "ats_type" in j
            assert "title" in j
            assert "company_name" in j
            assert "url" in j
            assert "ai_fit_score" in j
            assert "ai_fit_reasoning" in j
            assert "review_deadline" in j


# ---------------------------------------------------------------------
class TestStatusFilter:
    async def test_filter_in_review_returns_two(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs?status=in_review")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        for j in body["jobs"]:
            assert j["status"] == "in_review"

    async def test_filter_unknown_status_returns_empty(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs?status=ghosted-by-them")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 0
        assert body["jobs"] == []


# ---------------------------------------------------------------------
class TestPageSize:
    async def test_page_size_caps_returned_count_but_total_still_full(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        r = await seeded_jobs.get("/api/jobs?page_size=2")
        assert r.status_code == 200, r.text
        body = r.json()
        # ``jobs`` is the page slice.
        assert len(body["jobs"]) == 2
        # ``total`` reflects the matched set *before* slicing so the
        # React list can render "showing 2 of 6".
        assert body["total"] == 6


# ---------------------------------------------------------------------
class TestPendingCount:
    async def test_pending_count_matches_seeded_in_review_records(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        r = await seeded_jobs.get("/api/jobs/pending-count")
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 2

    async def test_pending_count_update_after_approve(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        before = (await seeded_jobs.get("/api/jobs/pending-count")).json()["count"]
        await seeded_jobs.post(f"/api/jobs/{J_1_ID}/approve")
        after = (await seeded_jobs.get("/api/jobs/pending-count")).json()["count"]
        assert after == before - 1


# ---------------------------------------------------------------------
class TestApprove:
    async def test_approve_flips_status_and_clears_deadline(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # Verify the seeded j_1 has a future deadline before approve.
        before = (await seeded_jobs.get("/api/jobs?status=in_review")).json()["jobs"]
        j1 = next(j for j in before if j["id"] == J_1_ID)
        assert j1["status"] == "in_review"
        assert j1["review_deadline"] is not None
        deadline = datetime.fromisoformat(
            j1["review_deadline"].replace("Z", "+00:00")
        )
        assert deadline > datetime.now(timezone.utc)

        r = await seeded_jobs.post(f"/api/jobs/{J_1_ID}/approve")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == J_1_ID
        assert body["status"] == "approved"
        assert body["review_deadline"] is None

    async def test_approve_missing_returns_404(self, seeded_jobs: AsyncClient) -> None:
        # Use a syntactically-valid UUID that doesn't exist.
        r = await seeded_jobs.post(
            "/api/jobs/00000000-0000-0000-0000-000000000000/approve"
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------
class TestReject:
    async def test_reject_flips_status_and_clears_deadline(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        r = await seeded_jobs.post(f"/api/jobs/{J_2_ID}/reject")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == J_2_ID
        assert body["status"] == "rejected"
        assert body["review_deadline"] is None

    async def test_reject_missing_returns_404(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.post(
            "/api/jobs/00000000-0000-0000-0000-000000000000/reject"
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------
class TestBadUuid:
    async def test_approve_with_non_uuid_string_returns_404(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # The route treats malformed-UUID input as "not found" rather
        # than 422 because the "did this row exist?" check is the
        # primary failure mode we want to surface in the operator log.
        r = await seeded_jobs.post("/api/jobs/not-a-uuid/approve")
        assert r.status_code == 404, r.text


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
class TestPatchJobStatus:
    """Canonical status writer. Updates ``jobs.status`` AND inserts a
    ``job_status_history`` row in the same transaction so a future
    audit query can never observe a status change with no history.
    """

    async def test_patch_to_approved_writes_history_row(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # Snapshot the in_review row's id so we can verify the
        # history row's job_id matches.
        j1_id = _seed_id_for("j_1")

        # Pre-state: j_1 is in_review with a future deadline.
        before = (await seeded_jobs.get("/api/jobs?status=in_review")).json()["jobs"]
        assert any(j["id"] == str(j1_id) for j in before)

        # PATCH j_1 → approved.
        r = await seeded_jobs.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "approved", "source": "user", "note": "looks great"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(j1_id)
        assert body["status"] == "approved"
        # ``approved`` is a terminal status → the review_deadline
        # should be cleared by the patch.
        assert body["review_deadline"] is None

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

        history = await _fetch_history()
        assert len(history) == 1
        h = history[0]
        assert h.from_status == "in_review"
        assert h.to_status == "approved"
        assert h.source == "user"
        assert h.note == "looks great"
        # ``changed_at`` is auto-stamped by the DB default.
        assert h.changed_at is not None

    async def test_patch_unknown_job_returns_404(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.patch(
            "/api/jobs/00000000-0000-0000-0000-000000000000/status",
            json={"status": "approved"},
        )
        assert r.status_code == 404, r.text

    async def test_patch_invalid_status_value_returns_422(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # Pydantic validates the Literal at the route's request-body
        # parsing step; an unknown status is a 422, not a 400.
        j1_id = _seed_id_for("j_1")
        r = await seeded_jobs.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "ghosted-by-them"},
        )
        assert r.status_code == 422, r.text

    async def test_patch_back_to_in_review_does_not_set_new_deadline(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # PATCH-ing an approved job back to in_review must NOT crash
        # and must not invent a deadline — the deadline was cleared
        # by the prior approve, and the route does not re-seed one
        # when transitioning back to a non-terminal status.
        j1_id = _seed_id_for("j_1")
        await seeded_jobs.post(f"/api/jobs/{j1_id}/approve")
        r = await seeded_jobs.patch(
            f"/api/jobs/{j1_id}/status",
            json={"status": "in_review"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "in_review"
        assert r.json()["review_deadline"] is None

    async def test_patch_default_source_is_user(self, seeded_jobs: AsyncClient) -> None:
        # No source field → defaults to ``user`` per the
        # ``JobStatusPatch`` Pydantic model. An operator-click is
        # the audit-trail default; only programmatic changes opt
        # into ``auto_apply`` or similar.
        j1_id = _seed_id_for("j_1")
        await seeded_jobs.patch(
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

        h = await _fetch_history()
        assert h.source == "user"


# ---------------------------------------------------------------------
class TestPostResearch:
    """Sync Interview Prep. The route calls ``LLMClient.from_env()``
    which is patched here to return a fixed Markdown brief so the
    test is deterministic and offline.
    """

    def _mock_llm_research(
        self,
        content: str = "## Company Snapshot\nstub brief",
        model: str = "meta/llama-3.1-70b-instruct",
    ):
        """Patch :class:`LLMClient.from_env` and the per-instance
        ``research_opportunity`` to return a fixed tuple.
        """
        # We need both from_env (class method) AND the research_opportunity
        # coroutine. Patch the class method on the LLMClient symbol that
        # ``routes.jobs`` imports.
        mock_client = MagicMock()
        mock_client.research_opportunity = AsyncMock(return_value=(content, model))
        return patch("routes.jobs.LLMClient.from_env", return_value=mock_client)

    async def test_research_happy_path_persists_report(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        j1_id = _seed_id_for("j_1")
        with self._mock_llm_research("## Company Snapshot\nreal content", "test-model"):
            r = await seeded_jobs.post(f"/api/jobs/{j1_id}/research")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["job_id"] == str(j1_id)
        assert body["status"] == "ready"
        assert body["content"] == "## Company Snapshot\nreal content"
        assert body["model_used"] == "test-model"
        assert body["error"] is None
        assert body["requested_at"] is not None
        assert body["generated_at"] is not None

        # DB assertion: the research_reports row exists with the
        # right job_id and content.
        async def _fetch_report() -> db_models.ResearchReport:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.ResearchReport)
                    .where(db_models.ResearchReport.job_id == j1_id)
                )
                return (await session.execute(stmt)).scalars().first()

        report = await _fetch_report()
        assert report is not None
        assert report.status == db_models.RESEARCH_STATUS_READY
        assert report.content == "## Company Snapshot\nreal content"
        assert report.model_used == "test-model"
        assert report.error is None

    async def test_research_unknown_job_returns_404(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.post(
            "/api/jobs/00000000-0000-0000-0000-000000000000/research"
        )
        assert r.status_code == 404, r.text

    async def test_research_llm_failure_persists_failed_row_and_returns_502(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # When every LLM provider fails, ``research_opportunity``
        # raises ``RuntimeError``. The route catches it, persists a
        # ``status='failed'`` research_reports row, and returns 502
        # so the React modal can surface the error verbatim.
        j1_id = _seed_id_for("j_1")
        mock_client = MagicMock()
        mock_client.research_opportunity = AsyncMock(
            side_effect=RuntimeError(
                "all LLM providers failed; last error type=APIConnectionError"
            )
        )
        with patch("routes.jobs.LLMClient.from_env", return_value=mock_client):
            r = await seeded_jobs.post(f"/api/jobs/{j1_id}/research")
        assert r.status_code == 502, r.text
        # The 502 detail carries the operator-visible error.
        assert "research failed" in r.json()["detail"].lower()

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

        reports = await _fetch_reports()
        assert len(reports) == 1
        rep = reports[0]
        assert rep.status == db_models.RESEARCH_STATUS_FAILED
        assert rep.content is None
        assert rep.model_used is None
        assert "APIConnectionError" in (rep.error or "")

    async def test_research_no_api_keys_persists_failed_row(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # ``LLMClient.from_env()`` itself raises ``RuntimeError``
        # when no API key is configured. The route catches that
        # the same way as a per-call LLM failure.
        j1_id = _seed_id_for("j_1")
        with patch(
            "routes.jobs.LLMClient.from_env",
            side_effect=RuntimeError("no LLM provider configured"),
        ):
            r = await seeded_jobs.post(f"/api/jobs/{j1_id}/research")
        assert r.status_code == 502, r.text

        async def _fetch_reports() -> list[db_models.ResearchReport]:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.ResearchReport)
                    .where(db_models.ResearchReport.job_id == j1_id)
                )
                return list((await session.execute(stmt)).scalars().all())

        reports = await _fetch_reports()
        assert len(reports) == 1
        assert reports[0].status == db_models.RESEARCH_STATUS_FAILED
        assert "no LLM provider" in (reports[0].error or "")


# ---------------------------------------------------------------------
class TestGetResearch:
    """GET /api/jobs/{id}/research re-opens the most recent ready
    report without a fresh LLM call. Used by the React modal's
    reload path.
    """

    async def test_get_research_with_no_reports_returns_404(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        j1_id = _seed_id_for("j_1")
        r = await seeded_jobs.get(f"/api/jobs/{j1_id}/research")
        assert r.status_code == 404, r.text

    async def test_get_research_returns_latest_ready_report(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        j1_id = _seed_id_for("j_1")
        # Stub two research calls — only the latest ready one is
        # returned.
        mock_client = MagicMock()
        mock_client.research_opportunity = AsyncMock(
            return_value=("brief v2", "test-model")
        )
        with patch("routes.jobs.LLMClient.from_env", return_value=mock_client):
            r1 = await seeded_jobs.post(f"/api/jobs/{j1_id}/research")
            assert r1.status_code == 200, r1.text
        # Second call should hit the cache.
        with patch("routes.jobs.LLMClient.from_env", return_value=mock_client):
            r2 = await seeded_jobs.post(f"/api/jobs/{j1_id}/research")
            assert r2.status_code == 200, r2.text
        # GET returns the most recent.
        r3 = await seeded_jobs.get(f"/api/jobs/{j1_id}/research")
        assert r3.status_code == 200, r3.text
        assert r3.json()["content"] == "brief v2"


# ---------------------------------------------------------------------
# v0.5 delivery: multi-status filter (?status=in_review,approved) +
# score range (score_min + score_max) so the React JobBoard can
# filter on a single slider without a separate dropdown.
# ---------------------------------------------------------------------
class TestMultiStatusFilter:
    """Comma-separated ?status= query param.

    The seed has 2x in_review, 1x approved, 1x rejected, 1x applied,
    1x flagged. The multi-status OR query should return the union of
    the requested sets, and the wire ``total`` should reflect the
    matched count *before* the page-size slice.
    """

    async def test_two_statuses_or_returns_union(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs?status=in_review,approved")
        assert r.status_code == 200, r.text
        body = r.json()
        # 2 in_review + 1 approved = 3
        assert body["total"] == 3
        seen = {j["status"] for j in body["jobs"]}
        assert seen.issubset({"in_review", "approved"})
        assert len(seen) > 0  # at least one of each

    async def test_three_statuses_or_returns_union(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs?status=in_review,approved,rejected")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 4

    async def test_all_five_statuses_returns_full_set(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get(
            "/api/jobs?status=in_review,approved,rejected,applied,flagged"
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 6

    async def test_unknown_in_list_short_circuits_to_empty(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # An unknown fragment anywhere in the comma-separated list
        # short-circuits the whole query — same contract as the
        # single-status path, so a typo at the call site can't
        # reach the SQLAlchemy ``status = ANY(...)`` with a bad
        # enum value.
        r = await seeded_jobs.get("/api/jobs?status=in_review,ghosted-by-them")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0
        assert r.json()["jobs"] == []

    async def test_single_status_still_works(self, seeded_jobs: AsyncClient) -> None:
        # Backward-compat: ?status=in_review (no comma) must still
        # return the in_review rows. The new path branches to the
        # old ``WHERE status = $1`` form for the single-value case
        # to keep the SQL plan cheap.
        r = await seeded_jobs.get("/api/jobs?status=in_review")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 2
        for j in r.json()["jobs"]:
            assert j["status"] == "in_review"


# ---------------------------------------------------------------------
class TestScoreRangeFilter:
    """score_min + score_max together form a half-open / closed range.

    Seed: scores are 0.86, 0.78, 0.91, 0.42, 0.74, 0.58. The default
    (0.0, 1.0) returns all 6. Tightening either bound drops rows.
    """

    async def test_default_range_returns_all(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 6

    async def test_score_min_floor_drops_below_threshold(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # >= 0.8 → keeps 0.86, 0.91 → 2 rows
        r = await seeded_jobs.get("/api/jobs?score_min=0.8")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        for j in body["jobs"]:
            assert j["ai_fit_score"] >= 0.8

    async def test_score_max_ceiling_drops_above_threshold(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # <= 0.5 → keeps 0.42 → 1 row
        r = await seeded_jobs.get("/api/jobs?score_max=0.5")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert body["jobs"][0]["ai_fit_score"] <= 0.5

    async def test_score_min_and_max_together(self, seeded_jobs: AsyncClient) -> None:
        # 0.5 <= score <= 0.8 → 0.78, 0.74, 0.58 → 3 rows
        r = await seeded_jobs.get("/api/jobs?score_min=0.5&score_max=0.8")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 3
        for j in body["jobs"]:
            assert 0.5 <= j["ai_fit_score"] <= 0.8

    async def test_score_min_out_of_range_returns_422(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # Pydantic / FastAPI's ``Query(ge=0.0, le=1.0)`` rejects
        # out-of-range values with 422 before the route body runs.
        r = await seeded_jobs.get("/api/jobs?score_min=1.5")
        assert r.status_code == 422, r.text

    async def test_score_max_out_of_range_returns_422(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        r = await seeded_jobs.get("/api/jobs?score_max=-0.1")
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------
# v0.5 wire-up: GET /api/jobs/{id} single-job lookup + ?company_id filter
# on the list endpoint. The single-job route powers the React
# ``JobDetail`` page; the company_id filter powers CompanyDetail's
# "Related Jobs" section.
# ---------------------------------------------------------------------
class TestGetJobById:
    async def test_get_job_by_id_returns_seeded_row(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        r = await seeded_jobs.get(f"/api/jobs/{J_1_ID}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == J_1_ID
        assert body["title"] == "Senior AI Engineer"
        assert body["company_name"] == "Replicate"
        assert body["status"] == "in_review"
        assert body["ai_fit_score"] == pytest.approx(0.86)

    async def test_get_job_missing_returns_404(self, seeded_jobs: AsyncClient) -> None:
        r = await seeded_jobs.get("/api/jobs/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404, r.text

    async def test_get_job_bad_uuid_returns_404(self, seeded_jobs: AsyncClient) -> None:
        # Same convention as the approve/reject/research routes:
        # a malformed UUID is treated as "not found" rather than
        # 422, so the operator log surfaces one consistent
        # failure mode for "row not present".
        r = await seeded_jobs.get("/api/jobs/not-a-uuid")
        assert r.status_code == 404, r.text

    async def test_get_job_envelope_shape(self, seeded_jobs: AsyncClient) -> None:
        body = (await seeded_jobs.get(f"/api/jobs/{J_1_ID}")).json()
        # The single-job response carries the same fields as a list
        # entry (the React JobDetail reads them all directly).
        for key in (
            "id", "status", "ats_type", "title", "company_name",
            "url", "ai_fit_score", "ai_fit_reasoning", "review_deadline",
        ):
            assert key in body


class TestListJobsCompanyIdFilter:
    """``?company_id=<uuid>`` filter on the list endpoint. None of
    the seed rows have ``company_id`` set, so the filter on the seed
    itself returns 0 rows. The next two tests install a company_id
    on j_1 and j_2 to exercise the happy + isolation paths.
    """

    async def test_no_company_id_param_returns_all(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # Backward-compat: omitting the param must not change the
        # existing list behavior (all 6 seed rows).
        r = await seeded_jobs.get("/api/jobs")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 6

    async def test_company_id_with_no_matching_jobs_returns_empty(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # None of the seed rows have a company_id, so any filter
        # value returns 0 rows.
        r = await seeded_jobs.get(
            "/api/jobs?company_id=00000000-0000-0000-0000-000000000000"
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0
        assert r.json()["jobs"] == []

    async def test_company_id_returns_only_matching_rows(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # Stamp j_1 + j_2 with the same company_id and re-query.
        test_company_id = _seed_id_for("company_for_test")

        async def _stamp() -> None:
            async with AsyncSessionLocal() as session:
                for marker in ("j_1", "j_2"):
                    jid = _seed_id_for(marker)
                    row = await session.get(db_models.Job, jid)
                    row.company_id = test_company_id
                    await session.flush()
                await session.commit()

        await _stamp()

        r = await seeded_jobs.get(f"/api/jobs?company_id={test_company_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        ids = {j["id"] for j in body["jobs"]}
        assert ids == {J_1_ID, J_2_ID}

    async def test_company_id_bad_uuid_returns_422(self, seeded_jobs: AsyncClient) -> None:
        # Pydantic / FastAPI's ``UUID`` query param rejects malformed
        # values with 422 before the route body runs.
        r = await seeded_jobs.get("/api/jobs?company_id=not-a-uuid")
        assert r.status_code == 422, r.text

    async def test_company_id_composes_with_status_filter(
        self, seeded_jobs: AsyncClient,
    ) -> None:
        # The new filter should compose with the existing multi-status
        # filter — the SQL builder treats them as an AND.
        test_company_id = _seed_id_for("company_for_test2")

        async def _stamp() -> None:
            async with AsyncSessionLocal() as session:
                j1 = await session.get(db_models.Job, _seed_id_for("j_1"))
                j3 = await session.get(db_models.Job, _seed_id_for("j_3"))
                j1.company_id = test_company_id  # in_review
                j3.company_id = test_company_id  # approved
                await session.flush()
                await session.commit()

        await _stamp()

        # ?status=in_review + ?company_id=<test> → only j_1.
        r = await seeded_jobs.get(
            f"/api/jobs?status=in_review&company_id={test_company_id}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert body["jobs"][0]["id"] == J_1_ID
