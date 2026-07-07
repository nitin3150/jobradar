"""Unit tests for the ``/api/outreach`` router.

Drives ``routes.outreach`` via FastAPI ``TestClient`` so the selection
math + message templates are exercised end-to-end against Pydantic
validation. The module-level ``_MESSAGES_DB`` is reset between tests so
assertions stay isolated.
"""
import unittest

from fastapi.testclient import TestClient

from main import app
from routes.outreach import _MESSAGES_DB


def _reset_db() -> None:
    _MESSAGES_DB.clear()


def _payload(type_: str, *, company_id: str = "co_42", skills=None, background=None):
    return {
        "company_id": company_id,
        "type": type_,
        "user_context": {
            "name": "Alice Engineer",
            "role": "Senior Engineer",
            "skills": skills if skills is not None else ["python", "fastapi"],
            "background": background,
        },
    }


class TestGenerateHappyPath(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.client = TestClient(app)

    def _post(self, type_):
        return self.client.post("/api/outreach/generate", json=_payload(type_))

    def test_email_returns_200_and_well_formed_message(self):
        r = self._post("email")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for key in (
            "id",
            "company_id",
            "type",
            "content",
            "created_at",
            "resume_picked_id",
            "resume_picked_name",
            "qa_snippet_id",
            "qa_snippet",
        ):
            self.assertIn(key, body, key)
        self.assertEqual(body["company_id"], "co_42")
        self.assertEqual(body["type"], "email")
        self.assertIn("Alice Engineer", body["content"])
        self.assertIn("co_42", body["content"])

    def test_twitter_dm_stays_within_280_chars(self):
        r = self._post("twitter_dm")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["type"], "twitter_dm")
        self.assertLessEqual(len(body["content"]), 280)

    def test_linkedin_stays_within_300_chars(self):
        r = self._post("linkedin")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["type"], "linkedin")
        self.assertLessEqual(len(body["content"]), 300)


class TestDeterministicSelection(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.client = TestClient(app)

    def test_python_fastapi_skills_picks_backend_resume(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=["python", "fastapi"]),
        )
        body = r.json()
        self.assertEqual(body["resume_picked_id"], "r2")
        self.assertEqual(body["resume_picked_name"], "backend-api.pdf")

    def test_ml_pytorch_skills_picks_ml_engineer_default(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=["ml", "python", "pytorch"]),
        )
        body = r.json()
        self.assertEqual(body["resume_picked_id"], "r1")

    def test_ml_skill_picks_ml_qa_snippet(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=["ml"]),
        )
        body = r.json()
        self.assertEqual(body["qa_snippet_id"], "q1")

    def test_backend_skill_picks_backend_qa_snippet(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=["backend"]),
        )
        body = r.json()
        self.assertEqual(body["qa_snippet_id"], "q2")

    def test_unmatched_skills_fall_back_to_default_qa(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=["rust", "zig"]),
        )
        body = r.json()
        self.assertEqual(body["qa_snippet_id"], "q1")

    def test_empty_skills_still_pick_a_resume(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=[]),
        )
        body = r.json()
        # Zero overlap everywhere — the ``is_default`` tie-break picks r1.
        self.assertEqual(body["resume_picked_id"], "r1")


class TestFetchMessages(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.client = TestClient(app)

    def test_get_unknown_company_returns_empty_list(self):
        r = self.client.get("/api/outreach/never-seen-company")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), [])

    def test_get_returns_messages_newest_first(self):
        ids = []
        for _ in range(2):
            r = self.client.post("/api/outreach/generate", json=_payload("email"))
            self.assertEqual(r.status_code, 200)
            ids.append(r.json()["id"])
        r = self.client.get("/api/outreach/co_42")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 2)
        # POSTed ids[0] first, ids[1] second; GET newest-first reverses.
        self.assertEqual(body[0]["id"], ids[1])
        self.assertEqual(body[1]["id"], ids[0])

    def test_get_filters_by_company_id(self):
        self.client.post(
            "/api/outreach/generate",
            json=_payload("email", company_id="co_a"),
        )
        self.client.post(
            "/api/outreach/generate",
            json=_payload("email", company_id="co_b"),
        )
        a = self.client.get("/api/outreach/co_a").json()
        b = self.client.get("/api/outreach/co_b").json()
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(a[0]["company_id"], "co_a")
        self.assertEqual(b[0]["company_id"], "co_b")


class TestPayloadValidation(unittest.TestCase):
    def setUp(self):
        _reset_db()
        self.client = TestClient(app)

    def test_missing_company_id_returns_422(self):
        body = _payload("email")
        del body["company_id"]
        r = self.client.post("/api/outreach/generate", json=body)
        self.assertEqual(r.status_code, 422)

    def test_missing_type_returns_422(self):
        body = _payload("email")
        del body["type"]
        r = self.client.post("/api/outreach/generate", json=body)
        self.assertEqual(r.status_code, 422)

    def test_invalid_type_returns_422(self):
        r = self.client.post(
            "/api/outreach/generate", json=_payload("tiktok"),
        )
        self.assertEqual(r.status_code, 422)

    def test_missing_user_context_returns_422(self):
        body = _payload("email")
        del body["user_context"]
        r = self.client.post("/api/outreach/generate", json=body)
        self.assertEqual(r.status_code, 422)

    def test_empty_company_id_returns_422(self):
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", company_id=""),
        )
        self.assertEqual(r.status_code, 422)


class TestRouterMount(unittest.TestCase):
    """Sanity-check the router sits under ``/api/outreach`` in the global app."""

    def setUp(self):
        _reset_db()
        self.client = TestClient(app)

    def test_openapi_lists_both_outreach_routes(self):
        spec = app.openapi()
        paths = sorted(spec["paths"].keys())
        self.assertIn("/api/outreach/generate", paths)
        # The GET path is parameterised in OpenAPI as ``/api/outreach/{company_id}``.
        self.assertTrue(
            any(p.startswith("/api/outreach/{company_id}") for p in paths),
            f"GET /api/outreach/{{company_id}} missing from {paths}",
        )

    def test_generate_via_global_app_round_trip(self):
        """The full stack — main.app + CORS + outreach + selection — works."""
        r = self.client.post(
            "/api/outreach/generate",
            json=_payload("email", skills=["ml", "pytorch"]),
        )
        self.assertEqual(r.status_code, 200, r.text)
        msg = r.json()
        self.assertEqual(msg["resume_picked_id"], "r1")
        self.assertEqual(msg["qa_snippet_id"], "q1")


if __name__ == "__main__":
    unittest.main()
