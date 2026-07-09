"""Unit tests for the ``/api/pipeline`` router.

Drives the route via ``fastapi.testclient.TestClient`` and patches
``routes.pipeline.scan_pipeline.invoke`` and ``routes.pipeline.run_jobs_boards``
so no real scrapers fire in CI. ``_reset_state()`` is called in every
``setUp`` so test pollution does not leak into neighboring tests.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
from routes.pipeline import _PIPELINE_STATE, _reset_state


def _fake_pipeline_result(*, funding: int = 0, remote: int = 0, ngos: int = 0, oss: int = 0) -> dict:
    """Build a LangGraph-state-shaped mock return value."""
    return {
        "input": "api",
        "funding": [{"id": f"f{i}"} for i in range(funding)],
        "remote": [{"id": f"r{i}"} for i in range(remote)],
        "ngos": [{"id": f"n{i}"} for i in range(ngos)],
        "oss": [{"id": f"o{i}"} for i in range(oss)],
    }


class _PipelineTestCase(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.client = TestClient(app)


# --------------------------------------------------------------------------
class TestStatus(_PipelineTestCase):
    def test_status_idle_before_any_run(self):
        r = self.client.get("/api/pipeline/status")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["state"], "idle")
        self.assertIsNone(body["last_run_at"])
        self.assertIsNone(body["last_run_counts"])
        self.assertIsNone(body["recent_error"])

    def test_status_reflects_last_run_after_post_run(self):
        with patch("routes.pipeline.scan_pipeline.invoke") as mock_invoke:
            mock_invoke.return_value = _fake_pipeline_result(funding=2, remote=3, ngos=1, oss=0)
            self.client.post("/api/pipeline/run")
        r = self.client.get("/api/pipeline/status")
        body = r.json()
        self.assertEqual(body["state"], "idle")
        self.assertEqual(body["last_run_counts"]["total"], 6)
        self.assertEqual(body["last_run_counts"]["funding"], 2)
        self.assertIsNotNone(body["last_run_at"])


# --------------------------------------------------------------------------
class TestPostRun(_PipelineTestCase):
    def test_post_run_returns_envelope_with_counts(self):
        with patch("routes.pipeline.scan_pipeline.invoke") as mock_invoke:
            mock_invoke.return_value = _fake_pipeline_result(funding=2, remote=3, ngos=1, oss=4)
            r = self.client.post("/api/pipeline/run")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["message"], "True")
        self.assertEqual(body["counts"]["funding"], 2)
        self.assertEqual(body["counts"]["remote"], 3)
        self.assertEqual(body["counts"]["ngos"], 1)
        self.assertEqual(body["counts"]["oss"], 4)
        self.assertEqual(body["counts"]["total"], 10)
        # Per-domain opportunity payloads present.
        self.assertEqual(len(body["opportunities"]["funding"]), 2)
        self.assertEqual(len(body["opportunities"]["oss"]), 4)
        # Duration is a positive number; ran_at is an ISO 8601 string.
        self.assertGreater(body["duration_seconds"], 0)
        self.assertIn("T", body["ran_at"])

    def test_post_run_concurrent_returns_409(self):
        _PIPELINE_STATE["state"] = "running"
        r = self.client.post("/api/pipeline/run")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already running", r.json()["detail"].lower())

    def test_post_run_handles_invoke_exception_returns_500_and_records_error(self):
        with patch("routes.pipeline.scan_pipeline.invoke") as mock_invoke:
            mock_invoke.side_effect = RuntimeError("simulated graph failure")
            r = self.client.post("/api/pipeline/run")
        self.assertEqual(r.status_code, 500, r.text)
        self.assertIn("simulated graph failure", r.json()["detail"].lower())
        # State reflects the failure for /status visibility.
        self.assertEqual(_PIPELINE_STATE["state"], "error")
        self.assertEqual(_PIPELINE_STATE["recent_error"], "simulated graph failure")


# --------------------------------------------------------------------------
class TestDiscover(_PipelineTestCase):
    def test_discover_returns_completed_with_attached_count(self):
        with patch("routes.pipeline.run_jobs_boards") as mock_run:
            mock_run.return_value = [{"id": "j1"}, {"id": "j2"}, {"id": "j3"}]
            # Stub scoring to a constant so the test doesn't try to
            # instantiate ``LLMClient.from_env()`` against the real env.
            with patch("routes.pipeline.score_and_persist") as mock_score:
                mock_score.return_value = 0
                r = self.client.get("/api/pipeline/discover")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["companies_attached"], 3)
        self.assertEqual(body["scanned"], 3)
        self.assertEqual(body["winners_count"], 0)
        self.assertIsNone(body["error"])

    def test_discover_scores_returned_jobs_with_boards_ats_type(self):
        """The boards dispatch path must run the LLM scoring service over
        every job returned by ``run_jobs_boards`` so winners land in the
        in-review queue without the operator having to call
        ``/api/scan/boards`` separately.
        """
        jobs = [
            {"id": "j1", "title": "Senior AI Engineer", "url": "https://a/1"},
            {"id": "j2", "title": "Junior Painter", "url": "https://b/2"},
        ]
        with patch("routes.pipeline.run_jobs_boards") as mock_run:
            mock_run.return_value = jobs
            with patch("routes.pipeline.score_and_persist") as mock_score:
                mock_score.return_value = 2
                r = self.client.get("/api/pipeline/discover")
        self.assertEqual(r.status_code, 200, r.text)
        mock_score.assert_called_once()
        call_args, _call_kwargs = mock_score.call_args
        # First positional arg is the opportunities list; second is the
        # ``ats_type`` discriminator used to namespace ``_JOBS_DB`` keys.
        self.assertEqual(call_args[0], jobs)
        self.assertEqual(call_args[1], "boards")

        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["winners_count"], 2)

    def test_discover_reports_zero_winners_when_scoring_returns_zero(self):
        """Stubs ``score_and_persist`` directly to verify the response
        contract: when scoring yields zero winners, the discover response
        stays ``status="completed"`` and ``winners_count`` mirrors the
        stub return value verbatim.
        """
        with patch("routes.pipeline.run_jobs_boards") as mock_run:
            mock_run.return_value = [{"id": "j1"}]
            with patch(
                "routes.pipeline.score_and_persist",
                return_value=0,
            ) as mock_score:
                r = self.client.get("/api/pipeline/discover")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["scanned"], 1)
        self.assertEqual(body["winners_count"], 0)
        mock_score.assert_called_once()

    def test_discover_records_recent_error_when_scoring_service_raises(self):
        """Defense-in-depth: if ``score_and_persist`` ever escapes its
        internal swallow and propagates (e.g., an ``asyncio.run`` bound
        to a running event loop, or a regression in the swallow), the
        discover endpoint must keep status="completed" because the boards
        scrape itself succeeded, but ``_PIPELINE_STATE["recent_error"]``
        must surface the scoring failure so the operator's
        ``/api/pipeline/status`` doesn't show a silent breakage.
        """
        with patch("routes.pipeline.run_jobs_boards") as mock_run:
            mock_run.return_value = [{"id": "j1"}]
            with patch(
                "routes.pipeline.score_and_persist",
                side_effect=RuntimeError("scoring service crashed"),
            ):
                r = self.client.get("/api/pipeline/discover")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Boards scrape succeeded; scoring crash doesn't degrade status.
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["scanned"], 1)
        self.assertEqual(body["winners_count"], 0)
        # But recent_error IS set so /status surfaces the failure.
        r_status = self.client.get("/api/pipeline/status").json()
        self.assertIn("scoring crashed after boards scrape", r_status["recent_error"] or "")

    def test_discover_reports_zero_winners_when_llm_provider_missing(self):
        """Genuinely exercises the missing-provider path: stubs
        ``services.scoring_service.LLMClient.from_env`` to raise the
        production ``RuntimeError`` that an empty env hits. Catches any
        regression where ``score_and_persist`` stops swallowing the
        missing-provider error and starts propagating it (which would
        degrade the discover response to status="failed" even though
        the boards scrape itself succeeded).
        """
        with patch("routes.pipeline.run_jobs_boards") as mock_run:
            mock_run.return_value = [
                {"id": "j1", "title": "Senior AI Engineer", "url": "https://a/1"},
                {"id": "j2", "title": "Junior Painter", "url": "https://b/2"},
            ]
            with patch(
                "services.llm_client.LLMClient.from_env",
                side_effect=RuntimeError("no LLM provider configured"),
            ):
                r = self.client.get("/api/pipeline/discover")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Boards scrape succeeded even though scoring couldn't run, so
        # status stays "completed" and winners_count is 0.
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["scanned"], 2)
        self.assertEqual(body["winners_count"], 0)
        self.assertIsNone(body["error"])

    def test_discover_propagates_failure_status_200_with_status_failed(self):
        with patch("routes.pipeline.run_jobs_boards") as mock_run:
            mock_run.side_effect = RuntimeError("boards runner down")
            r = self.client.get("/api/pipeline/discover")
        # We return 200 with ``status: "failed"`` so the operator-friendly
        # form is easier to render than a 5xx — the React ScheduleControl
        # branches on ``res.status === "completed"``.
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "boards runner down")
        self.assertEqual(body["companies_attached"], 0)
        self.assertEqual(body["winners_count"], 0)

    def test_discover_returns_409_when_state_running(self):
        _PIPELINE_STATE["state"] = "running"
        r = self.client.get("/api/pipeline/discover")
        self.assertEqual(r.status_code, 409)


# --------------------------------------------------------------------------
class TestSchedule(_PipelineTestCase):
    def test_get_schedule_default_1h_with_legal_options(self):
        r = self.client.get("/api/pipeline/schedule")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["interval_hours"], 1)
        self.assertEqual(body["options"], [1, 2, 4, 6, 12, 24])
        self.assertIsNotNone(body["next_run"])

    def test_put_schedule_valid_updates_state(self):
        r = self.client.put("/api/pipeline/schedule", json={"interval_hours": 12})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["interval_hours"], 12)
        self.assertIsNotNone(body["updated_at"])
        # State-level check — future calls see the new value.
        self.assertEqual(_PIPELINE_STATE["interval_hours"], 12)

    def test_get_schedule_after_put_reflects_new_interval(self):
        self.client.put("/api/pipeline/schedule", json={"interval_hours": 6})
        r = self.client.get("/api/pipeline/schedule")
        self.assertEqual(r.json()["interval_hours"], 6)

    def test_put_schedule_legal_values_round_trip(self):
        for hours in [1, 2, 4, 6, 12, 24]:
            with self.subTest(hours=hours):
                _reset_state()
                r = self.client.put(
                    "/api/pipeline/schedule", json={"interval_hours": hours},
                )
                self.assertEqual(r.status_code, 200, f"hours={hours}: {r.text}")
                self.assertEqual(r.json()["interval_hours"], hours)

    def test_put_schedule_invalid_hours_returns_422(self):
        for bad in [5, 25, 0, -1, 100]:
            with self.subTest(bad=bad):
                r = self.client.put(
                    "/api/pipeline/schedule", json={"interval_hours": bad},
                )
                self.assertEqual(r.status_code, 422, f"bad={bad}: {r.text}")


# --------------------------------------------------------------------------
class TestStats(_PipelineTestCase):
    def test_get_stats_total_companies_sourced_from_companies_db(self):
        # The seeded /api/companies store has 6 records with exactly one
        # ``category == "ngos"`` (Open Climate Fix, c_5).
        r = self.client.get("/api/pipeline/stats")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total_companies"], 6)
        self.assertEqual(body["ngo_count"], 1)
        # Placeholder tiles for the demo until persistence + scheduler land.
        self.assertEqual(body["new_today"], 0)
        self.assertEqual(body["high_intent"], 0)
        self.assertEqual(body["contacted"], 0)


# --------------------------------------------------------------------------
class TestRouterMount(_PipelineTestCase):
    def test_openapi_lists_all_six_pipeline_routes(self):
        spec = app.openapi()
        paths = sorted(
            p for p in spec["paths"].keys() if p.startswith("/api/pipeline")
        )
        # 5 unique paths; ``/api/pipeline/schedule`` has both GET and PUT so
        # the operations count is 6, matching the user spec.
        self.assertEqual(len(paths), 5, f"saw {paths}")
        operations = sorted(
            (method.upper(), path)
            for path in paths
            for method in spec["paths"][path].keys()
        )
        self.assertEqual(len(operations), 6, f"saw {operations}")
        # Every documented (method, path) pair is reachable from OpenAPI.
        for required in (
            ("POST", "/api/pipeline/run"),
            ("GET",  "/api/pipeline/status"),
            ("GET",  "/api/pipeline/discover"),
            ("GET",  "/api/pipeline/schedule"),
            ("PUT",  "/api/pipeline/schedule"),
            ("GET",  "/api/pipeline/stats"),
        ):
            self.assertIn(required, operations, f"{required!r} missing from {operations}")


if __name__ == "__main__":
    unittest.main()
