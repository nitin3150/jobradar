"""Tests for :mod:`routes.applications` — exercises the wire shape the
React ``ApplicationTracker`` page consumes + the new
``POST /api/applications`` manual-apply handoff endpoint.

Mirrors the pattern from :mod:`tests.test_jobs`: real Postgres-backed
store, ``_seed_applications()`` truncates + reseeds between tests so
mutations from one test don't leak into the next. The new POST tests
also seed a job row (via :func:`routes.jobs._seed_job_rows`) so the
FK path can be exercised end-to-end.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from db import models as db_models
from db.session import AsyncSessionLocal
from main import app
from routes.applications import _seed_applications, _seed_id_for
from routes.jobs import _seed_job_rows


def _run(coro):
    """Helper to drive an async coroutine from a sync test body.

    Most tests stay synchronous (FastAPI's ``TestClient`` runs routes
    in a worker thread, so the test thread itself never enters an
    event loop). Use this for the setUp/tearDown hooks that need to
    truncate + reseed the ``applications`` table or to peek at DB
    state.
    """
    return asyncio.run(coro)


def _session():
    """Helper to open a fresh AsyncSession — the test framework runs
    everything synchronously so we open + close around each setUp.
    """
    return AsyncSessionLocal()


# Pre-generated UUIDs for the seeded ids so tests can address them by
# path string. Mirror of ``_seed_id_for`` in routes.applications —
# keeping it here so test failures point at a stable, redacted-from-
# import URL.
A_1_ID = str(_seed_id_for("a_1"))
A_2_ID = str(_seed_id_for("a_2"))
A_3_ID = str(_seed_id_for("a_3"))
A_4_ID = str(_seed_id_for("a_4"))
A_5_ID = str(_seed_id_for("a_5"))
A_6_ID = str(_seed_id_for("a_6"))


class _ApplicationsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Reseed the applications table to the canonical 6 fixtures.
        # The new POST tests also need a job row, but that lives in
        # ``setUpJobSeed`` (a separate mixin below) so the list/PATCH
        # tests don't pay for an unnecessary ``_seed_job_rows`` write.
        _run(_seed_applications(_session()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        from db.session import AsyncSessionLocal as _ASL

        async def _wipe_applications() -> None:
            async with _ASL() as session:
                await session.execute(sa_delete(db_models.Application))
                await session.commit()

        _run(_wipe_applications())


class _JobSeedMixin:
    """Mix-in: also seed the ``jobs`` table (canonical 6 fixture rows).

    Used by the new POST tests because the endpoint requires a real
    Job row to attach the Application to. The list/PATCH tests don't
    need this and skip the cost.
    """

    def _seed_jobs(self) -> None:
        _run(_seed_job_rows(_session()))

    def _wipe_jobs(self) -> None:
        async def _wipe() -> None:
            async with AsyncSessionLocal() as session:
                await session.execute(sa_delete(db_models.Job))
                await session.commit()

        _run(_wipe())

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self._seed_jobs()

    def tearDown(self) -> None:  # type: ignore[override]
        self._wipe_jobs()
        super().tearDown()  # type: ignore[misc]


# ---------------------------------------------------------------------------
class TestList(_ApplicationsTestCase):
    def test_get_returns_every_seed_record(self) -> None:
        r = self.client.get("/api/applications")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)
        ids = {a["id"] for a in body["applications"]}
        self.assertEqual(
            ids, {A_1_ID, A_2_ID, A_3_ID, A_4_ID, A_5_ID, A_6_ID}
        )

    def test_envelope_shape(self) -> None:
        body = self.client.get("/api/applications").json()
        for app in body["applications"]:
            self.assertIn("id", app)
            self.assertIn("job_id", app)
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
        self.assertEqual(body["applications"][0]["id"], A_6_ID)
        # Full id-order pin: a future seed mutation that reorders or
        # adds a row surfaces here, before the React UI silently shifts.
        self.assertEqual(
            [a["id"] for a in body["applications"]],
            [A_6_ID, A_1_ID, A_2_ID, A_5_ID, A_3_ID, A_4_ID],
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
        self.assertEqual(body["id"], A_1_ID)
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
        async def _fetch_notes() -> str | None:
            async with AsyncSessionLocal() as session:
                stmt = select(db_models.Application).where(
                    db_models.Application.id == _seed_id_for("a_2")
                )
                row = (await session.execute(stmt)).scalars().first()
                return row.notes if row else None

        original_notes = _run(_fetch_notes())
        self.assertIsNotNone(original_notes)
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

    def test_patch_with_unknown_status_returns_422(self) -> None:
        r = self.client.patch(
            "/api/applications/a_1/status",
            json={"status": "maybe-yes"},
        )
        self.assertEqual(r.status_code, 422, r.text)


# ---------------------------------------------------------------------------
# POST /api/applications — manual-apply handoff endpoint
# ---------------------------------------------------------------------------
# Pre-generated UUIDs for the canonical seeded job rows. The POST
# tests use these to look up jobs in the seeded ``jobs`` table; see
# :mod:`routes.jobs._seed_job_rows` for the schema.
J_1_ID = str(_seed_id_for("j_1"))  # in_review
J_2_ID = str(_seed_id_for("j_2"))  # in_review
J_3_ID = str(_seed_id_for("j_3"))  # approved
J_4_ID = str(_seed_id_for("j_4"))  # rejected
J_5_ID = str(_seed_id_for("j_5"))  # applied
J_6_ID = str(_seed_id_for("j_6"))  # flagged


class TestCreateApplicationFromJob(_JobSeedMixin, _ApplicationsTestCase):
    """Happy path + state-machine + 404 + 422 coverage for the new
    ``POST /api/applications`` manual-apply handoff endpoint.

    Uses the ``_JobSeedMixin`` so the seeded ``jobs`` table is
    available for the FK lookup inside the route. The mixin's
    ``setUp`` calls ``super().setUp()`` which calls
    ``_seed_applications`` so the list/PATCH fixtures are also
    present (the POST tests don't read them but the seed gives a
    known baseline).
    """

    def test_post_creates_application_and_flips_job_to_applied(self) -> None:
        # Pre-state: j_3 is approved.
        async def _job_status(jid: str) -> str | None:
            async with AsyncSessionLocal() as session:
                row = await session.get(db_models.Job, UUID(jid))
                return row.status if row else None

        self.assertEqual(_run(_job_status(J_3_ID)), "approved")

        r = self.client.post(
            "/api/applications",
            json={"job_id": J_3_ID, "notes": "Applied via LinkedIn Easy Apply."},
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        # New application row carries the operator's notes + the job's
        # title / company_name + status=submitted + NULL screenshot.
        self.assertEqual(body["job_id"], J_3_ID)
        self.assertEqual(body["job_title"], "Backend Engineer")
        self.assertEqual(body["company_name"], "Vercel")
        self.assertEqual(body["status"], "submitted")
        self.assertEqual(body["notes"], "Applied via LinkedIn Easy Apply.")
        self.assertIsNone(body["submission_screenshot_path"])
        # submitted_at is the moment the operator clicked Mark as
        # applied, within a small slack of the wall clock.
        submitted = datetime.fromisoformat(
            body["submitted_at"].replace("Z", "+00:00")
        )
        self.assertLess(
            abs((datetime.now(timezone.utc) - submitted).total_seconds()), 5
        )
        # Job row flipped to 'applied' in the same transaction.
        self.assertEqual(_run(_job_status(J_3_ID)), "applied")
        # Verify the new row is queryable via GET /api/applications.
        list_body = self.client.get("/api/applications").json()
        new_ids = {a["id"] for a in list_body["applications"]}
        self.assertIn(body["id"], new_ids)

    def test_post_without_notes_leaves_notes_null(self) -> None:
        r = self.client.post(
            "/api/applications", json={"job_id": J_3_ID},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIsNone(r.json()["notes"])

    def test_post_for_missing_job_returns_404(self) -> None:
        # Syntactically-valid UUID that doesn't exist in the seeded
        # jobs table.
        r = self.client.post(
            "/api/applications",
            json={"job_id": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(r.status_code, 404, r.text)

    def test_post_for_malformed_uuid_returns_404(self) -> None:
        # The route treats malformed-UUID input as "not found" rather
        # than 422 because the "did this row exist?" check is the
        # primary failure mode we want to surface in the operator log.
        r = self.client.post(
            "/api/applications", json={"job_id": "not-a-uuid"},
        )
        self.assertEqual(r.status_code, 404, r.text)

    def test_post_for_in_review_job_returns_409(self) -> None:
        r = self.client.post(
            "/api/applications", json={"job_id": J_1_ID},
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("approved", r.json()["detail"])

    def test_post_for_rejected_job_returns_409(self) -> None:
        r = self.client.post(
            "/api/applications", json={"job_id": J_4_ID},
        )
        self.assertEqual(r.status_code, 409, r.text)

    def test_post_for_already_applied_job_returns_409(self) -> None:
        """Idempotency guard against a double-click of Mark as applied.

        The seeded j_5 starts in status='applied', so the first POST
        (which would 409 because the state machine rejects anything
        but 'approved') actually 409s here too — but for a different
        reason. We also exercise the case where the operator approves
        j_3, marks it applied (success), then immediately clicks
        Mark as applied again (409 because j_3 is now 'applied').
        """
        # Case 1: j_5 is already 'applied' from the seed — POST
        # returns 409 with the "in status 'applied'" reason.
        r = self.client.post(
            "/api/applications", json={"job_id": J_5_ID},
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("'applied'", r.json()["detail"])

        # Case 2: approve j_3, mark applied (success), then POST
        # again — 409 because the second call sees status='applied'.
        self.client.post(f"/api/jobs/{J_3_ID}/approve")
        first = self.client.post(
            "/api/applications", json={"job_id": J_3_ID},
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            "/api/applications", json={"job_id": J_3_ID},
        )
        self.assertEqual(second.status_code, 409, second.text)

    def test_post_without_job_id_field_returns_422(self) -> None:
        r = self.client.post("/api/applications", json={})
        self.assertEqual(r.status_code, 422, r.text)

    def test_post_for_flagged_job_returns_409(self) -> None:
        r = self.client.post(
            "/api/applications", json={"job_id": J_6_ID},
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("'flagged'", r.json()["detail"])

    def test_post_writes_job_status_history_row(self) -> None:
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
        from sqlalchemy import select

        # Pre-state: no history rows for j_3.
        async def _history_count() -> int:
            async with AsyncSessionLocal() as session:
                stmt = select(db_models.JobStatusHistory).where(
                    db_models.JobStatusHistory.job_id == UUID(J_3_ID)
                )
                return len(list((await session.execute(stmt)).scalars().all()))

        self.assertEqual(_run(_history_count()), 0)

        r = self.client.post(
            "/api/applications",
            json={"job_id": J_3_ID, "notes": "Applied via LinkedIn Easy Apply."},
        )
        self.assertEqual(r.status_code, 201, r.text)

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

        history = _run(_fetch_history())
        self.assertEqual(len(history), 1)
        h = history[0]
        self.assertEqual(h.from_status, "approved")
        self.assertEqual(h.to_status, "applied")
        self.assertEqual(h.source, db_models.JOB_STATUS_SOURCE_USER)
        self.assertEqual(h.note, "Applied via LinkedIn Easy Apply.")
        self.assertIsNotNone(h.changed_at)


if __name__ == "__main__":
    unittest.main()
