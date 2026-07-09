"""Tests for :mod:`routes.applications` — exercises the wire shape
the React ``ApplicationTracker`` page consumes + the new
``POST /api/applications`` manual-apply handoff endpoint.

Mirrors the pattern from :mod:`tests.test_jobs`: real
Postgres-backed store, async fixtures in :mod:`conftest` truncate
+ reseed the relevant table(s) between tests so mutations from one
test don't leak into the next. The new POST tests also seed a job
row (via :func:`seeded_jobs_and_applications`) so the FK path can
be exercised end-to-end.

Why async + httpx.AsyncClient (was unittest.TestCase + TestClient):
the previous ``setUp`` called ``asyncio.run(_seed_applications(...))``
to populate the DB from a sync test body. With pytest-asyncio
installed, the session loop is alive in the test thread, so
``asyncio.run()`` raises ``RuntimeError: cannot be called from a
running event loop``. Async fixtures + async test methods keep
every coroutine on the same loop the runner provides.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from db import models as db_models
from db.session import AsyncSessionLocal
from routes.applications import _seed_id_for


# Pre-generated UUIDs for the seeded ids so tests can address them
# by path string. Mirror of ``_seed_id_for`` in routes.applications
# — keeping it here so test failures point at a stable,
# redacted-from-import URL.
A_1_ID = str(_seed_id_for("a_1"))
A_2_ID = str(_seed_id_for("a_2"))
A_3_ID = str(_seed_id_for("a_3"))
A_4_ID = str(_seed_id_for("a_4"))
A_5_ID = str(_seed_id_for("a_5"))
A_6_ID = str(_seed_id_for("a_6"))


# Pre-generated UUIDs for the canonical seeded job rows. The POST
# tests use these to look up jobs in the seeded ``jobs`` table; see
# :mod:`routes.jobs._seed_job_rows` for the schema.
J_1_ID = str(_seed_id_for("j_1"))  # in_review
J_2_ID = str(_seed_id_for("j_2"))  # in_review
J_3_ID = str(_seed_id_for("j_3"))  # approved
J_4_ID = str(_seed_id_for("j_4"))  # rejected
J_5_ID = str(_seed_id_for("j_5"))  # applied
J_6_ID = str(_seed_id_for("j_6"))  # flagged


# ---------------------------------------------------------------------------
class TestList:
    async def test_get_returns_every_seed_record(
        self, seeded_applications: AsyncClient,
    ) -> None:
        r = await seeded_applications.get("/api/applications")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 6
        ids = {a["id"] for a in body["applications"]}
        assert ids == {A_1_ID, A_2_ID, A_3_ID, A_4_ID, A_5_ID, A_6_ID}

    async def test_envelope_shape(self, seeded_applications: AsyncClient) -> None:
        body = (await seeded_applications.get("/api/applications")).json()
        for app in body["applications"]:
            assert "id" in app
            assert "job_id" in app
            assert "job_title" in app
            assert "company_name" in app
            assert "submitted_at" in app
            assert "status" in app
            assert "last_email_at" in app
            assert "submission_screenshot_path" in app
            assert "notes" in app

    async def test_status_filter_returns_only_matching(
        self, seeded_applications: AsyncClient,
    ) -> None:
        r = await seeded_applications.get("/api/applications?status=interview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        for app in body["applications"]:
            assert app["status"] == "interview"

    async def test_status_filter_unknown_returns_empty(
        self, seeded_applications: AsyncClient,
    ) -> None:
        body = (
            await seeded_applications.get("/api/applications?status=offrd-by-them")
        ).json()
        assert body["total"] == 0
        assert body["applications"] == []

    async def test_list_sorted_by_submitted_at_desc(
        self, seeded_applications: AsyncClient,
    ) -> None:
        body = (await seeded_applications.get("/api/applications")).json()
        timestamps = [a["submitted_at"] for a in body["applications"]]
        # Newest first; ISO 8601 lex sort equals chronological sort
        # because all timestamps end in ``Z`` (UTC) at second precision.
        assert timestamps == sorted(timestamps, reverse=True)
        # The seeded ``a_6`` was submitted 1 day ago, the most recent.
        assert body["applications"][0]["id"] == A_6_ID
        # Full id-order pin: a future seed mutation that reorders or
        # adds a row surfaces here, before the React UI silently shifts.
        assert [a["id"] for a in body["applications"]] == [
            A_6_ID, A_1_ID, A_2_ID, A_5_ID, A_3_ID, A_4_ID,
        ]


# ---------------------------------------------------------------------------
class TestPageSize:
    async def test_page_size_caps_count_but_total_reflects_full_match(
        self, seeded_applications: AsyncClient,
    ) -> None:
        body = (
            await seeded_applications.get("/api/applications?page_size=2")
        ).json()
        assert len(body["applications"]) == 2
        assert body["total"] == 6


# ---------------------------------------------------------------------------
class TestPatchStatus:
    async def test_patch_flips_status(self, seeded_applications: AsyncClient) -> None:
        r = await seeded_applications.patch(
            f"/api/applications/{A_1_ID}/status",
            json={"status": "interview"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == A_1_ID
        assert body["status"] == "interview"

    async def test_patch_appends_notes(self, seeded_applications: AsyncClient) -> None:
        r = await seeded_applications.patch(
            f"/api/applications/{A_1_ID}/status",
            json={"status": "submitted", "notes": "Recruiter call notes go here."},
        )
        assert r.status_code == 200, r.text
        assert r.json()["notes"] == "Recruiter call notes go here."

    async def test_patch_omitting_notes_leaves_them_untouched(
        self, seeded_applications: AsyncClient,
    ) -> None:
        # Seeded a_2 already has notes; a PATCH without notes should not wipe them.
        async def _fetch_notes() -> str | None:
            async with AsyncSessionLocal() as session:
                stmt = select(db_models.Application).where(
                    db_models.Application.id == _seed_id_for("a_2")
                )
                row = (await session.execute(stmt)).scalars().first()
                return row.notes if row else None

        original_notes = await _fetch_notes()
        assert original_notes is not None
        r = await seeded_applications.patch(
            f"/api/applications/{A_2_ID}/status", json={"status": "ghosted"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["notes"] == original_notes

    async def test_patch_missing_returns_404(self, seeded_applications: AsyncClient) -> None:
        r = await seeded_applications.patch(
            "/api/applications/does-not-exist/status",
            json={"status": "rejected"},
        )
        assert r.status_code == 404, r.text

    async def test_patch_with_unknown_status_returns_422(
        self, seeded_applications: AsyncClient,
    ) -> None:
        r = await seeded_applications.patch(
            "/api/applications/a_1/status",
            json={"status": "maybe-yes"},
        )
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# POST /api/applications — manual-apply handoff endpoint
# ---------------------------------------------------------------------------
class TestCreateApplicationFromJob:
    """Happy path + state-machine + 404 + 422 coverage for the new
    ``POST /api/applications`` manual-apply handoff endpoint.

    Takes the ``seeded_jobs_and_applications`` fixture so both the
    seeded ``jobs`` table (for the FK lookup) AND the seeded
    ``applications`` table (for the list-after-POST assertions) are
    available. This replaces the previous
    ``class TestCreateApplicationFromJob(_JobSeedMixin, _ApplicationsTestCase)``
    mixin pattern, which is awkward to express in pytest — a fixture
    composition is the canonical replacement.
    """

    async def test_post_creates_application_and_flips_job_to_applied(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications

        # Pre-state: j_3 is approved.
        async def _job_status(jid: str) -> str | None:
            async with AsyncSessionLocal() as session:
                row = await session.get(db_models.Job, UUID(jid))
                return row.status if row else None

        assert await _job_status(J_3_ID) == "approved"

        r = await client.post(
            "/api/applications",
            json={"job_id": J_3_ID, "notes": "Applied via LinkedIn Easy Apply."},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # New application row carries the operator's notes + the job's
        # title / company_name + status=submitted + NULL screenshot.
        assert body["job_id"] == J_3_ID
        assert body["job_title"] == "Backend Engineer"
        assert body["company_name"] == "Vercel"
        assert body["status"] == "submitted"
        assert body["notes"] == "Applied via LinkedIn Easy Apply."
        assert body["submission_screenshot_path"] is None
        # submitted_at is the moment the operator clicked Mark as
        # applied, within a small slack of the wall clock.
        submitted = datetime.fromisoformat(
            body["submitted_at"].replace("Z", "+00:00")
        )
        assert abs((datetime.now(timezone.utc) - submitted).total_seconds()) < 5
        # Job row flipped to 'applied' in the same transaction.
        assert await _job_status(J_3_ID) == "applied"
        # Verify the new row is queryable via GET /api/applications.
        list_body = (await client.get("/api/applications")).json()
        new_ids = {a["id"] for a in list_body["applications"]}
        assert body["id"] in new_ids

    async def test_post_without_notes_leaves_notes_null(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        r = await client.post(
            "/api/applications", json={"job_id": J_3_ID},
        )
        assert r.status_code == 201, r.text
        assert r.json()["notes"] is None

    async def test_post_for_missing_job_returns_404(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        # Syntactically-valid UUID that doesn't exist in the seeded
        # jobs table.
        r = await client.post(
            "/api/applications",
            json={"job_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert r.status_code == 404, r.text

    async def test_post_for_malformed_uuid_returns_404(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        # The route treats malformed-UUID input as "not found" rather
        # than 422 because the "did this row exist?" check is the
        # primary failure mode we want to surface in the operator log.
        r = await client.post(
            "/api/applications", json={"job_id": "not-a-uuid"},
        )
        assert r.status_code == 404, r.text

    async def test_post_for_in_review_job_returns_409(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        r = await client.post(
            "/api/applications", json={"job_id": J_1_ID},
        )
        assert r.status_code == 409, r.text
        assert "approved" in r.json()["detail"]

    async def test_post_for_rejected_job_returns_409(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        r = await client.post(
            "/api/applications", json={"job_id": J_4_ID},
        )
        assert r.status_code == 409, r.text

    async def test_post_for_already_applied_job_returns_409(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        """Idempotency guard against a double-click of Mark as applied.

        The seeded j_5 starts in status='applied', so the first POST
        (which would 409 because the state machine rejects anything
        but 'approved') actually 409s here too — but for a different
        reason. We also exercise the case where the operator approves
        j_3, marks it applied (success), then immediately clicks
        Mark as applied again (409 because j_3 is now 'applied').
        """
        client = seeded_jobs_and_applications

        # Case 1: j_5 is already 'applied' from the seed — POST
        # returns 409 with the "in status 'applied'" reason.
        r = await client.post(
            "/api/applications", json={"job_id": J_5_ID},
        )
        assert r.status_code == 409, r.text
        assert "'applied'" in r.json()["detail"]

        # Case 2: approve j_3, mark applied (success), then POST
        # again — 409 because the second call sees status='applied'.
        await client.post(f"/api/jobs/{J_3_ID}/approve")
        first = await client.post(
            "/api/applications", json={"job_id": J_3_ID},
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            "/api/applications", json={"job_id": J_3_ID},
        )
        assert second.status_code == 409, second.text

    async def test_post_without_job_id_field_returns_422(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        r = await client.post("/api/applications", json={})
        assert r.status_code == 422, r.text

    async def test_post_for_flagged_job_returns_409(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        client = seeded_jobs_and_applications
        r = await client.post(
            "/api/applications", json={"job_id": J_6_ID},
        )
        assert r.status_code == 409, r.text
        assert "'flagged'" in r.json()["detail"]

    async def test_post_writes_job_status_history_row(
        self, seeded_jobs_and_applications: AsyncClient,
    ) -> None:
        """The manual-apply handoff must write a ``job_status_history``
        row in the same transaction as the Application INSERT + Job
        status flip. The audit-trail contract is: a future observer
        can never see ``status='applied'`` without a matching
        history row, and vice versa.

        Without this, the ``GET /api/jobs?status=in_review`` queue
        count and the ``GET /api/jobs/{id}/research`` (latest report)
        path would have a silent drift — the operator clicks Mark as
        applied, the job moves to ``applied``, the history table
        doesn't, and the next "what did the user do to job X?"
        audit query has no answer.
        """
        client = seeded_jobs_and_applications

        # Pre-state: no history rows for j_3.
        async def _history_count() -> int:
            async with AsyncSessionLocal() as session:
                stmt = select(db_models.JobStatusHistory).where(
                    db_models.JobStatusHistory.job_id == UUID(J_3_ID)
                )
                return len(list((await session.execute(stmt)).scalars().all()))

        assert await _history_count() == 0

        r = await client.post(
            "/api/applications",
            json={"job_id": J_3_ID, "notes": "Applied via LinkedIn Easy Apply."},
        )
        assert r.status_code == 201, r.text

        # Post-state: one history row, from='approved', to='applied',
        # source='user' (operator manual click), note carried through.
        async def _fetch_history() -> list[db_models.JobStatusHistory]:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(db_models.JobStatusHistory)
                    .where(db_models.JobStatusHistory.job_id == UUID(J_3_ID))
                    .order_by(db_models.JobStatusHistory.changed_at.asc())
                )
                return list((await session.execute(stmt)).scalars().all())

        history = await _fetch_history()
        assert len(history) == 1
        h = history[0]
        assert h.from_status == "approved"
        assert h.to_status == "applied"
        assert h.source == db_models.JOB_STATUS_SOURCE_USER
        assert h.note == "Applied via LinkedIn Easy Apply."
        assert h.changed_at is not None
