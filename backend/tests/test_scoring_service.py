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

Step-3 note: ``build_profile_summary`` now delegates to
:func:`services.profile_service.build_profile_summary`, so the
profile content in tests comes from the committed
``config/profile.example.yml`` (no QA bank involvement). The
profile-service module-level cache is reset in :meth:`asyncSetUp`
so a cached profile from one test never bleeds into the next.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete as sa_delete, select

from db import models as db_models
from db.session import AsyncSessionLocal
from routes.settings import _PREFS_STATE, _reset_prefs
from services.profile_service import reset_cache

from services.scoring_service import (
    _score_and_persist_async,
    apply_visa_calibration,
    build_profile_summary,
    extract_visa_flag,
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
        _reset_prefs()  # reset _PREFS_STATE (in-memory)
        reset_cache()  # reset the profile_service module-level cache
        await _truncate_jobs_table()

    async def asyncTearDown(self) -> None:
        reset_cache()
        await _truncate_jobs_table()


# ---------------------------------------------------------------------
class TestBuildProfileSummary(unittest.TestCase):
    """``build_profile_summary`` is a pure-function delegate to the
    profile_service renderer — no DB, no LLM, no async.

    Deliberately NOT inheriting from :class:`_ScoringTestCase` (which
    truncates the ``jobs`` table in asyncSetUp) because the profile
    rendering has nothing to do with the DB. The autouse ``setUp``
    resets the profile_service module-level cache so a cached
    profile from a previous test file (e.g. test_profile_service.py
    saving a mocked "Logged" candidate) doesn't bleed into this
    class's assertions.
    """

    def setUp(self) -> None:
        reset_cache()

    def test_includes_target_roles_from_profile_yaml(self) -> None:
        # The example profile has these as primary target roles;
        # the test environment has no operator profile.yml so the
        # example is the fallback. ``build_profile_summary`` should
        # render the fit-grouped block ("Primary (dream roles):" etc.)
        # from the profile_service renderer, NOT the old
        # "Target roles:\n- X\n- Y" bullet list.
        text = build_profile_summary()
        self.assertIn("Target roles:", text)
        self.assertIn("Senior AI Engineer", text)
        # The new format groups by fit level so the LLM understands
        # priority ordering.
        self.assertIn("Primary (dream roles):", text)

    def test_does_not_include_qa_summary(self) -> None:
        # Step-3 regression guard: the Q&A bank is no longer in
        # the scoring prompt. A test that the section header
        # never appears, regardless of what the operator has
        # put in the Q&A bank (the bank itself still exists for
        # application form auto-fill — see routes.qa_bank).
        text = build_profile_summary()
        self.assertNotIn("Q&A summary:", text)
        self.assertNotIn("Q: Years of experience", text)
        self.assertNotIn("(no answer yet)", text)

    def test_includes_narrative_from_profile_yaml(self) -> None:
        # The example profile has a headline + superpowers; the
        # new renderer surfaces these in the LLM prompt so the
        # scorer can use narrative context (not just role titles).
        text = build_profile_summary()
        # headline: "ML Engineer turned AI product builder"
        self.assertIn("Headline:", text)
        self.assertIn("ML Engineer", text)

    def test_renders_empty_profile_safely(self) -> None:
        # If neither profile.yml nor profile.example.yml exists,
        # build_profile_summary returns "(no profile configured)".
        # We don't delete the example here (it's committed to the
        # repo); this test just confirms the sentinel path doesn't
        # raise. The non-existent-file case is covered in
        # test_profile_service.py::TestLoadProfile.
        text = build_profile_summary()
        # We expect the example to be present, so the sentinel
        # should NOT appear. The assertion is "non-empty
        # string starting with 'Target roles:' or 'Headline:'".
        self.assertNotEqual(text, "(no profile configured)")
        self.assertTrue(text.startswith("Target roles:") or "Headline:" in text)


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
        # Single-threshold rule: above-threshold winners land as
        # ``approved`` directly (apply worker picks them up on its
        # next polling tick). No ``in_review`` intermediate.
        self.assertEqual(row.status, "approved")
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




# ---------------------------------------------------------------------
class TestExtractVisaFlag(unittest.TestCase):
    """``extract_visa_flag`` strips the ``visa_flag:X`` prefix the LLM
    prefixes onto its ``reasoning`` value.

    The regex anchors at the START of the string and tolerates any
    separator between the tag and the prose (``-``, `` ``, em/en dash,
    ``:``). Case-insensitive on the keyword + value.
    """

    def test_positive_with_em_dash_separator(self) -> None:
        flag, body = extract_visa_flag(
            "visa_flag:positive \u2014 job explicitly says we will sponsor"
        )
        self.assertEqual(flag, "positive")
        self.assertEqual(body, "job explicitly says we will sponsor")

    def test_positive_with_hyphen_separator(self) -> None:
        flag, body = extract_visa_flag(
            "visa_flag:positive - we sponsor visas for the right candidate"
        )
        self.assertEqual(flag, "positive")
        self.assertEqual(body, "we sponsor visas for the right candidate")

    def test_negative_with_colon_separator(self) -> None:
        flag, body = extract_visa_flag(
            "visa_flag:negative: posting says no sponsorship available"
        )
        self.assertEqual(flag, "negative")
        self.assertEqual(body, "posting says no sponsorship available")

    def test_ambiguous_and_none_values(self) -> None:
        self.assertEqual(
            extract_visa_flag("visa_flag:ambiguous - status may be considered")[0],
            "ambiguous",
        )
        self.assertEqual(
            extract_visa_flag("visa_flag:none - description lacks any mention")[0],
            "none",
        )

    def test_case_insensitive_keyword_and_value(self) -> None:
        flag, _ = extract_visa_flag("VISA_FLAG:POSITIVE - uppercased prefix")
        self.assertEqual(flag, "positive")
        flag, _ = extract_visa_flag("Visa_Flag:Negative - mixed case")
        self.assertEqual(flag, "negative")

    def test_no_prefix_returns_none_and_unchanged_reasoning(self) -> None:
        # No ``visa_flag:`` prefix at start -> identity mapping.
        flag, body = extract_visa_flag(
            "Strong role fit but the posting mentions \"we will sponsor\"."
        )
        self.assertIsNone(flag)
        self.assertEqual(
            body,
            "Strong role fit but the posting mentions \"we will sponsor\".",
        )

    def test_empty_input(self) -> None:
        self.assertEqual(extract_visa_flag(""), (None, ""))

    def test_visa_flag_in_middle_of_string_does_not_match(self) -> None:
        # Only the START of the reasoning counts. A model that put
        # the tag mid-sentence would need to re-format its output,
        # not retrofit an out-of-band interpretation. Anchor honesty.
        flag, body = extract_visa_flag(
            "Mid-sentence prefix visa_flag:positive - trailing prose"
        )
        self.assertIsNone(flag)
        self.assertEqual(body, "Mid-sentence prefix visa_flag:positive - trailing prose")


# ---------------------------------------------------------------------
class TestApplyVisaCalibration(unittest.TestCase):
    """``apply_visa_calibration`` is the deterministic post-processor
    that hardens the LLM's score against visa-signal drift.

    The matrix is small (3 visa_status classes x 4 visa_flag values)
    but each branch is a candidate-regression site, so each branch
    is pinned by its own test method.
    """

    def test_negative_flag_for_needy_candidate_is_clamped_to_floor(self) -> None:
        # Candidate needs sponsorship, LLM emitted negative flag,
        # LLM score 0.85 (model over-estimated). Cap at 0.2.
        self.assertAlmostEqual(
            apply_visa_calibration(0.85, "negative", "No sponsorship"),
            0.2,
            places=4,
        )

    def test_negative_flag_low_score_stays_low(self) -> None:
        # Already below the floor; clamp should not raise it.
        self.assertAlmostEqual(
            apply_visa_calibration(0.1, "negative", "No sponsorship"),
            0.1,
            places=4,
        )

    def test_positive_flag_for_needy_candidate_is_boosted(self) -> None:
        # Candidate needs sponsorship, LLM emitted positive flag.
        self.assertAlmostEqual(
            apply_visa_calibration(0.7, "positive", "No sponsorship"),
            0.75,
            places=4,
        )

    def test_positive_flag_boost_is_capped(self) -> None:
        # Already near 1.0; boost must not overflow above 0.95.
        self.assertAlmostEqual(
            apply_visa_calibration(0.93, "positive", "No sponsorship"),
            0.95,
            places=4,
        )

    def test_ambiguous_flag_for_needy_candidate_leaves_score(self) -> None:
        # LLM couldn't tell \u2014 don't move the score on visa alone.
        self.assertAlmostEqual(
            apply_visa_calibration(0.85, "ambiguous", "No sponsorship"),
            0.85,
            places=4,
        )

    def test_none_flag_for_needy_candidate_leaves_score(self) -> None:
        # LLM saw no visa signal \u2014 don't move the score on visa alone.
        self.assertAlmostEqual(
            apply_visa_calibration(0.85, "none", "No sponsorship"),
            0.85,
            places=4,
        )

    def test_authorized_candidate_is_unaffected_by_any_flag(self) -> None:
        # ``No sponsorship needed`` (e.g. US citizen / GC holder)
        # means visa clauses are informational only. Score stands.
        for flag in ("positive", "negative", "ambiguous", "none"):
            with self.subTest(flag=flag):
                self.assertAlmostEqual(
                    apply_visa_calibration(0.85, flag, "No sponsorship needed"),
                    0.85,
                    places=4,
                )

    def test_unknown_visa_status_leaves_score(self) -> None:
        # Operator hasn't filled in a visa_status yet \u2014 the scorer
        # shouldn't pretend to know.
        self.assertAlmostEqual(
            apply_visa_calibration(0.85, "negative", None),
            0.85,
            places=4,
        )

    def test_unknown_flag_leaves_score(self) -> None:
        # LLM hygiene failure: tag missing or unparseable.
        self.assertAlmostEqual(
            apply_visa_calibration(0.85, None, "No sponsorship"),
            0.85,
            places=4,
        )

    def test_both_unknown_leaves_score(self) -> None:
        self.assertAlmostEqual(
            apply_visa_calibration(0.85, None, None),
            0.85,
            places=4,
        )

    def test_score_outside_unit_interval_is_clamped(self) -> None:
        # Belt-and-suspenders guards; the LLM is told 0.0-1.0 but if
        # it produces 1.5 we still cap at 1.0 on the way out.
        self.assertAlmostEqual(
            apply_visa_calibration(1.5, "none", "No sponsorship"),
            1.0,
            places=4,
        )
        self.assertAlmostEqual(
            apply_visa_calibration(-0.5, "negative", "No sponsorship"),
            0.0,
            places=4,
        )


# ---------------------------------------------------------------------
class TestScoreOneVisaWiring(_ScoringTestCase):
    """The visa_flag wiring inside ``_score_one`` must:

    * extract the tag from the LLM reasoning,
    * apply the calibration matrix,
    * persist the cleaned (tag-stripped) reasoning to the DB.

    This class mocks ``LLMClient.score_opportunity`` to return a
    realistic raw LLM response with ``visa_flag:`` prefix and asserts
    the persisted column is the cleaned reasoning (no tag) and that
    the score is calibrated.
    """

    @patch("services.scoring_service.LLMClient.from_env")
    async def test_negative_flag_clamped_persisted_row_uses_cleaned_reasoning(
        self, from_env_mock
    ):
        fake_client = AsyncMock()
        # LLM incorrectly returned 0.85 for a job that EXPLICITLY
        # blocks sponsorship. The wiring must:
        # 1) extract visa_flag:negative
        # 2) apply_visa_calibration clamps the score to 0.20
        # Threshold is 0.6 (default), so the row is dropped on
        # threshold grounds \u2014 confirm 0 rows persisted.
        fake_client.score_opportunity = AsyncMock(
            return_value=(
                0.85,
                "visa_flag:negative - this role requires US citizenship.",
            )
        )
        from_env_mock.return_value = fake_client

        result = await _score_and_persist_async(
            [
                {
                    "title": "Senior ML Engineer",
                    "company_name": "CitizensOnlyCorp",
                    "url": "https://citizensonly.co/jobs/1",
                }
            ],
            ats_type="boards",
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            await _row_count_for_url("https://citizensonly.co/jobs/1"), 0
        )

    @patch("services.scoring_service.LLMClient.from_env")
    async def test_positive_flag_boosted_persisted_row_uses_cleaned_reasoning(
        self, from_env_mock
    ):
        fake_client = AsyncMock()
        # LLM returned 0.70 for a job with visa_flag:positive and the
        # calibration matrix boosts the score by 0.05 (cap 0.95).
        # After boost = 0.75, which is above the 0.6 default threshold,
        # so the row persists. The persisted ai_fit_reasoning must be
        # the cleaned string (no tag prefix).
        fake_client.score_opportunity = AsyncMock(
            return_value=(
                0.70,
                "visa_flag:positive - we will sponsor visas for this role.",
            )
        )
        from_env_mock.return_value = fake_client

        result = await _score_and_persist_async(
            [
                {
                    "title": "Senior ML Engineer",
                    "company_name": "SponsorCo",
                    "url": "https://sponsor.co/jobs/1",
                }
            ],
            ats_type="boards",
        )
        self.assertEqual(result, 1)

        async with AsyncSessionLocal() as session:
            stmt = select(db_models.Job).where(
                db_models.Job.url == "https://sponsor.co/jobs/1"
            )
            rows = (await session.execute(stmt)).scalars().all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Calibration boosted 0.70 -> 0.75. Allow ±0.0001 rounding.
        self.assertAlmostEqual(row.ai_fit_score, 0.75, places=4)
        # The persisted reasoning has the visa_flag prefix STRIPPED
        # \u2014 the operator sees only the prose when filtering on it.
        self.assertNotIn("visa_flag:", row.ai_fit_reasoning)
        self.assertIn("we will sponsor visas for this role", row.ai_fit_reasoning)


if __name__ == "__main__":
    unittest.main()
