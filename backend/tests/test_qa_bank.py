"""Tests for :mod:`routes.qa_bank` — exercises the wire shape the React
``QABank`` page consumes.

Mirror of ``test_companies.py`` conventions: in-memory seeded store,
``_seed()`` reset in setUp.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from main import app
from routes.qa_bank import SHORT_TEXT_LIMIT, _QA_DB, _seed


class _QABankTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _seed()
        self.client = TestClient(app)


# ---------------------------------------------------------------------------
class TestListSeeded(_QABankTestCase):
    def test_get_returns_six_seed_records(self) -> None:
        r = self.client.get("/api/qa-bank")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)
        ids = {e["id"] for e in body["entries"]}
        self.assertEqual(ids, {"q1", "q2", "q3", "q4", "q5", "q6"})

    def test_envelope_shape(self) -> None:
        body = self.client.get("/api/qa-bank").json()
        for entry in body["entries"]:
            self.assertIn("id", entry)
            self.assertIn("question_pattern", entry)
            self.assertIn("canonical_question", entry)
            self.assertIn("answer", entry)
            self.assertIn("answer_type", entry)
            self.assertIn("times_used", entry)
            self.assertIn(entry["answer_type"], ("short_text", "long_text"))

    def test_sorted_by_times_used_desc(self) -> None:
        # The seed has q1 (14), q6 (11), q3 (7); top of the list should
        # be q1 → q6 → q3 (the three seeded answer-having entries).
        body = self.client.get("/api/qa-bank").json()
        ids = [e["id"] for e in body["entries"]]
        self.assertEqual(ids[:3], ["q1", "q6", "q3"])


# ---------------------------------------------------------------------------
class TestCreate(_QABankTestCase):
    def test_post_without_answer_defaults_to_short_text(self) -> None:
        r = self.client.post(
            "/api/qa-bank",
            json={
                "question_pattern": "remote preference",
                "canonical_question": "Remote work preference",
                # answer omitted
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["answer_type"], "short_text")
        self.assertIsNone(body["answer"])
        self.assertEqual(body["times_used"], 0)

    def test_post_short_answer_kept_as_short_text(self) -> None:
        short_answer = "x" * (SHORT_TEXT_LIMIT - 1)
        r = self.client.post(
            "/api/qa-bank",
            json={
                "question_pattern": "github user",
                "canonical_question": "GitHub username",
                "answer": short_answer,
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["answer_type"], "short_text")
        self.assertEqual(r.json()["answer"], short_answer)

    def test_post_long_answer_promoted_to_long_text(self) -> None:
        long_answer = "x" * (SHORT_TEXT_LIMIT + 1)
        r = self.client.post(
            "/api/qa-bank",
            json={
                "question_pattern": "background",
                "canonical_question": "Background",
                "answer": long_answer,
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["answer_type"], "long_text")

    def test_post_at_boundary_short_text_threshold(self) -> None:
        # Exactly ``SHORT_TEXT_LIMIT`` chars is the last short-text bucket.
        boundary = "x" * SHORT_TEXT_LIMIT
        r = self.client.post(
            "/api/qa-bank",
            json={
                "question_pattern": "linkedin headline",
                "canonical_question": "LinkedIn headline",
                "answer": boundary,
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(r.json()["answer"]), SHORT_TEXT_LIMIT)
        self.assertEqual(r.json()["answer_type"], "short_text")

    def test_post_whitespace_answer_normalizes_to_null(self) -> None:
        # POST and PATCH must agree: empty / whitespace-only → ``None``,
        # so the React UI's ``entry.answer || '⚠'`` orange highlight fires.
        r = self.client.post(
            "/api/qa-bank",
            json={
                "question_pattern": "phone",
                "canonical_question": "Phone",
                "answer": "   ",
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIsNone(r.json()["answer"])
        self.assertEqual(r.json()["answer_type"], "short_text")

    def test_post_lowercases_question_pattern(self) -> None:
        r = self.client.post(
            "/api/qa-bank",
            json={
                "question_pattern": "  Years Of Experience  ",
                "canonical_question": "Years Of Experience",
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["question_pattern"], "years of experience")
        # Canonical keeps the user's casing.
        self.assertEqual(body["canonical_question"], "Years Of Experience")


# ---------------------------------------------------------------------------
class TestPatch(_QABankTestCase):
    def test_patch_answer_re_derives_answer_type(self) -> None:
        # q1 starts short_text. Patch with a long answer.
        long_answer = "x" * (SHORT_TEXT_LIMIT + 50)
        r = self.client.patch(
            "/api/qa-bank/q1", json={"answer": long_answer},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["answer"], long_answer)
        self.assertEqual(body["answer_type"], "long_text")

    def test_patch_with_empty_answer_normalizes_to_null(self) -> None:
        # q1 currently has a real answer; setting answer="" should null it.
        r = self.client.patch("/api/qa-bank/q1", json={"answer": "   "})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["answer"])
        # Even with no answer, the entry stays in the dict (the React UI
        # then highlights it orange); answer_type auto-derives to short_text.
        self.assertEqual(r.json()["answer_type"], "short_text")

    def test_patch_lowercases_question_pattern(self) -> None:
        r = self.client.patch(
            "/api/qa-bank/q4", json={"question_pattern": "  ReLoCaTe WiLLiNG  "},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["question_pattern"], "relocate willing")

    def test_patch_missing_returns_404(self) -> None:
        r = self.client.patch(
            "/api/qa-bank/does-not-exist", json={"answer": "hi"},
        )
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------------
class TestDelete(_QABankTestCase):
    def test_delete_removes_record(self) -> None:
        self.assertIn("q2", _QA_DB)
        r = self.client.delete("/api/qa-bank/q2")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("q2", _QA_DB)
        # Body is the deleted record so any future onSuccess that wants
        # it for cache reconciliation has it.
        self.assertEqual(r.json()["id"], "q2")

    def test_delete_missing_returns_404(self) -> None:
        r = self.client.delete("/api/qa-bank/does-not-exist")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
