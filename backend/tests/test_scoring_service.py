"""Tests for :mod:`services.scoring_service` — profile composition and
threshold-filter persistence against the real Postgres ``jobs`` table.

We mock :class:`LLMClient.from_env` so the tests never hit NVIDIA / Groq
and are deterministic. The score returned per opportunity is parameterised
by the test, so we can verify that the threshold filter top/bottom boundary
is preserved.

These tests call :func:`_score_and_persist_async` directly (the inner
async pipeline) instead of the sync :func:`score_and_persist` wrapper —
because the sync wrapper uses ``asyncio.run`` internally and would
conflict with the per-test event loop that
:class:`unittest.IsolatedAsyncioTestCase` provides.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete as sa_delete, select

from db import models as db_models
from db.session import AsyncSessionLocal
from routes.qa_bank import _QA_DB, _seed as _seed_qa
from routes.settings import _PREFS_STATE, _reset_prefs

from services.scoring_service import (
    MAX_QA_ENTRIES_IN_PROMPT,
    _score_and_persist_async,
    build_profile_summary,
)


async def _truncate_jobs_table() -> None:
    """Wipe the ``jobs`` table — used in setUp to guarantee a clean slate.

    The scoring service writes via upsert so even rows from a previous
    test would survive into the current test, masking regressions
    (e.g. "test expects 1 new row but a previous test left a winner
    here"). Each test owns the table.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(db_models.Job))
        await session.commit()


async def _row_count_for_url(url: str) -> int:
    """Test-only helper — how many Job rows have this URL persisted?"""
    async with AsyncSessionLocal() as session:
        stmt = select(db_models.Job).where(db_models.Job.url == url)
        return len((await session.execute(stmt)).scalars().all())


class _ScoringTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _seed_qa()  # reset _QA_DB to canonical seed records (in-memory)
        _reset_prefs()  # reset _PREFS_STATE (in-memory)
        await _truncate_jobs_table()

    async def asyncTearDown(self) -> None:
        await _truncate_jobs_table()


# ---------------------------------------------------------------------
class TestBuildProfileSummary(_ScoringTestCase):
    def test_includes_target_roles(self) -> None:
        text = build_profile_summary()
        self.assertIn("Target roles:", text)
        self.assertIn("AI Engineer", text)
        self.assertIn("LLM Engineer", text)

    def test_includes_qa_with_answers(self) -> None:
        text = build_profile_summary()
        self.assertIn("Q&A summary:", text)
        self.assertIn("Years of experience", text)
        self.assertIn("5 years of professional software engineering", text)

    def test_truncates_huge_qa_answers(self) -> None:
        _QA_DB["q_huge"] = {
            "id": "q_huge",
            "question_pattern": "huge",
            "canonical_question": "Tell me everything",
            "answer": "x" * 1_000_000,
            "answer_type": "long_text",
            "times_used": 99,
        }
        text = build_profile_summary()
        # Cap is 200 chars per entry; the answer line stays bounded even
        # though the source answer is 1M chars.
        self.assertLess(len(text), 5_000)

    def test_includes_unanswered_qa_entries_with_sentinel(self) -> None:
        text = build_profile_summary()
        # q2 / q4 / q5 are seeded with answer=None. They should still
        # appear in the summary so the LLM knows they exist.
        self.assertIn("(no answer yet)", text)

    def test_caps_qa_entries_count(self) -> None:
        # Public constant for the upstream prompt builder.
        self.assertEqual(MAX_QA_ENTRIES_IN_PROMPT, 12)


# ---------------------------------------------------------------------
class TestPersistAboveThreshold(_ScoringTestCase):
    @patch("services.scoring_service.LLMClient.from_env")
    async def test_winner_is_persisted_to_jobs_table(self, from_env_mock):
        fake_client = AsyncMock()
        fake_client.score_opportunity = AsyncMock(return_value=(0.85, "Strong match"))
        from_env_mock.return_value = fake_client

        result = await _score_and_persist_async(
            [
                {
                    "title": "Senior AI Engineer",
                    "company_name": "Replicate",
                    "url": "https://rep.co/jobs/1",
                }
            ],
            ats_type="boards",
        )
        self.assertEqual(result, 1)

        # Verify exactly one row landed; assert field correctness.
        async with AsyncSessionLocal() as session:
            stmt = select(db_models.Job).where(
                db_models.Job.url == "https://rep.co/jobs/1"
            )
            rows = (await session.execute(stmt)).scalars().all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "in_review")
        self.assertAlmostEqual(row.ai_fit_score, 0.85, places=4)
        self.assertEqual(row.ai_fit_reasoning, "Strong match")
        self.assertEqual(row.ats_type, "boards")
        self.assertEqual(row.title, "Senior AI Engineer")
        self.assertEqual(row.company_name, "Replicate")


# ---------------------------------------------------------------------
class TestPersistBelowThreshold(_ScoringTestCase):
    @patch("services.scoring_service.LLMClient.from_env")
    async def test_loser_is_silently_dropped(self, from_env_mock):
        fake_client = AsyncMock()
        fake_client.score_opportunity = AsyncMock(
            return_value=(0.1, "Below preferred threshold")
        )
        from_env_mock.return_value = fake_client

        result = await _score_and_persist_async(
            [
                {
                    "title": "Junior Painter",
                    "company_name": "LocalCo",
                    "url": "https://local.co/jobs/1",
                }
            ],
            ats_type="boards",
        )
        self.assertEqual(result, 0)

        # Loser should not have landed in the jobs table.
        self.assertEqual(
            await _row_count_for_url("https://local.co/jobs/1"), 0
        )


# ---------------------------------------------------------------------
class TestThresholdRespected(_ScoringTestCase):
    @patch("services.scoring_service.LLMClient.from_env")
    async def test_threshold_picks_correct_outcomes(self, from_env_mock):
        # Tighten threshold to 0.9 — only scores >= 0.9 should pass.
        _PREFS_STATE["data"]["job_fit_threshold"] = 0.9

        fake_client = AsyncMock()
        fake_client.score_opportunity = AsyncMock(
            side_effect=[
                (0.5, "below"),    # below new threshold
                (0.95, "above"),   # above
            ]
        )
        from_env_mock.return_value = fake_client

        result = await _score_and_persist_async(
            [
                {"title": "Mid", "company_name": "Co", "url": "https://c/mid"},
                {"title": "Top", "company_name": "Co", "url": "https://c/top"},
            ],
            ats_type="boards",
        )
        self.assertEqual(result, 1)

        # Verify exactly the "Top" row landed; "Mid" was dropped.
        async with AsyncSessionLocal() as session:
            stmt = select(db_models.Job)
            rows = (await session.execute(stmt)).scalars().all()
        by_url = {r.url: r for r in rows}
        self.assertNotIn("https://c/mid", by_url)
        self.assertIn("https://c/top", by_url)
        self.assertAlmostEqual(by_url["https://c/top"].ai_fit_score, 0.95)


# ---------------------------------------------------------------------
class TestIdempotenceOnRerun(_ScoringTestCase):
    @patch("services.scoring_service.LLMClient.from_env")
    async def test_rerun_does_not_double_insert(self, from_env_mock):
        fake_client = AsyncMock()
        fake_client.score_opportunity = AsyncMock(return_value=(0.88, "good"))
        from_env_mock.return_value = fake_client

        opp = {"title": "X", "company_name": "Y", "url": "https://y/x"}
        await _score_and_persist_async([opp], ats_type="boards")
        await _score_and_persist_async([opp], ats_type="boards")

        # Rerun must upsert (same id) rather than append a clone.
        async with AsyncSessionLocal() as session:
            stmt = select(db_models.Job).where(
                db_models.Job.url == "https://y/x"
            )
            rows = (await session.execute(stmt)).scalars().all()
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------
class TestMissingAPIKey(_ScoringTestCase):
    @patch(
        "services.scoring_service.LLMClient.from_env",
        side_effect=RuntimeError("no LLM provider"),
    )
    async def test_returns_zero_without_exploding(self, from_env_mock):
        result = await _score_and_persist_async(
            [{"title": "x", "company_name": "y", "url": "https://z"}],
            ats_type="boards",
        )
        self.assertEqual(result, 0)
        self.assertEqual(await _row_count_for_url("https://z"), 0)


# ---------------------------------------------------------------------
class TestLLMFailureForOneOpportunity(_ScoringTestCase):
    @patch("services.scoring_service.LLMClient.from_env")
    async def test_per_opportunity_failure_does_not_block_others(
        self, from_env_mock
    ):
        fake_client = AsyncMock()
        # First call raises (simulates NVIDIA timeout); second succeeds.
        fake_client.score_opportunity = AsyncMock(
            side_effect=[
                RuntimeError("transient"),
                (0.8, "fine"),
            ]
        )
        from_env_mock.return_value = fake_client

        result = await _score_and_persist_async(
            [
                {"title": "Boom", "company_name": "X", "url": "https://x/boom"},
                {"title": "Ok", "company_name": "Y", "url": "https://y/ok"},
            ],
            ats_type="boards",
        )
        self.assertEqual(result, 1)

        async with AsyncSessionLocal() as session:
            stmt = select(db_models.Job)
            rows = (await session.execute(stmt)).scalars().all()
        by_url = {r.url: r for r in rows}
        self.assertNotIn("https://x/boom", by_url)
        self.assertIn("https://y/ok", by_url)
        self.assertEqual(by_url["https://y/ok"].title, "Ok")


if __name__ == "__main__":
    unittest.main()
