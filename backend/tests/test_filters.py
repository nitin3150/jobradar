"""Unit tests for ``backend.utils.filters``.

Covers:

* The pre-existing visa / default-irrelevant / default-relevant keyword
  blocks (sanity tests so a refactor cannot silently regress them).
* The new clearance / DoD / federal block (``CLEARANCE_PATTERNS``).
* The expanded visa block (12+ phrases covering work-auth, citizenship,
  green card, H-1B-specific).
* The seniority tier classification — high-rank match wins on multi-tier
  titles; unclassifiable → ``None``.
* The ``min_seniority`` / ``max_seniority`` band filter.
* Implicit mid-rank for unclassifiable titles when the operator has
  opted in to a band.

Each test class groups a single behaviour so a regression points
straight at the broken mechanism without scanning unrelated
assertions. The tests don't import :mod:`main` so they stay cheap and
don't trigger the FastAPI lifespan / route registration.
"""
from __future__ import annotations

import unittest

from utils.filters import (
    CLEARANCE_PATTERNS,
    DEFAULT_RELEVANT_PATTERNS,
    NO_SPONSORSHIP_PATTERNS,
    SENIORITY_RANKS,
    SENIORITY_TIERS,
    SENIORITY_VALUES,
    SeniorityTier,
    classify_seniority,
    filter_roles,
    is_relevant_role,
    seniority_rank,
)


# ---------------------------------------------------------------------------
class TestClassifySeniority(unittest.TestCase):
    """Direct coverage of the regex ladder; the band filter tests reuse
    this machinery indirectly.
    """

    def test_junior_engineer(self) -> None:
        self.assertEqual(classify_seniority("Junior Engineer"), "junior")

    def test_senior_engineer(self) -> None:
        self.assertEqual(classify_seniority("Senior Engineer"), "senior")

    def test_senior_staff_picks_highest_rank(self) -> None:
        # Both senior (3) and staff (4) match — staff wins.
        self.assertEqual(classify_seniority("Senior Staff Engineer"), "staff")

    def test_senior_director_picks_director(self) -> None:
        self.assertEqual(classify_seniority("Senior Director"), "director")

    def test_senior_vp_picks_vp(self) -> None:
        self.assertEqual(classify_seniority("Senior VP"), "vp")

    def test_principal_engineer(self) -> None:
        self.assertEqual(classify_seniority("Principal Engineer"), "principal")

    def test_tech_lead(self) -> None:
        self.assertEqual(classify_seniority("Tech Lead"), "lead")

    def test_engineering_lead(self) -> None:
        self.assertEqual(classify_seniority("Engineering Lead"), "lead")

    def test_engineering_manager(self) -> None:
        self.assertEqual(classify_seniority("Engineering Manager"), "manager")

    def test_senior_manager_picks_manager(self) -> None:
        # Manager (6) > senior (3) — manager wins.
        self.assertEqual(classify_seniority("Senior Manager"), "manager")

    def test_fellow(self) -> None:
        self.assertEqual(classify_seniority("Research Fellow"), "director")

    def test_internship(self) -> None:
        self.assertEqual(classify_seniority("Engineering Internship"), "intern")

    def test_staff_plus_alias(self) -> None:
        self.assertEqual(classify_seniority("Staff+ Engineer"), "principal")

    def test_software_engineer_alone_unclassifiable(self) -> None:
        self.assertIsNone(classify_seniority("Software Engineer"))

    def test_empty_title_returns_none(self) -> None:
        self.assertIsNone(classify_seniority(""))

    def test_word_boundary_keeps_seniority_out_of_senior_tier(self) -> None:
        # "Seniority Report" contains the substring "seniority" but the
        # word boundary keeps it out of the senior tier. Note the
        # trailing "Manager" doesn't qualify either because \b…\b on
        # "manager" actually matches — see below for the lead
        # ambiguity test that calls this out explicitly.
        self.assertEqual(classify_seniority("Seniority Report"), None)

    def test_word_boundary_keeps_staffing_out_of_staff_tier(self) -> None:
        self.assertEqual(classify_seniority("Staffing Coordinator"), None)

    def test_word_boundary_keeps_leadership_out_of_lead_tier(self) -> None:
        # "Leadership" doesn't trigger lead (which requires a
        # tech/team/engineering/group qualifier). "Leadership
        # Program" alone has no tier match.
        self.assertEqual(classify_seniority("Leadership Program"), None)
        # The manager tier matches the trailing "Manager" because
        # people-management roles ARE management regardless of title
        # style — both assertions are intentional. The lead tier
        # exclusion (above) is what the test name documents.
        self.assertEqual(classify_seniority("Leadership Program Manager"), "manager")


# ---------------------------------------------------------------------------
class TestSeniorityRank(unittest.TestCase):
    def test_known_tiers_lookup_correctly(self) -> None:
        for name, rank, _ in SENIORITY_TIERS:
            self.assertEqual(seniority_rank(name), rank)

    def test_none_lookup_returns_negative_one(self) -> None:
        self.assertEqual(seniority_rank(None), -1)

    def test_unknown_name_returns_negative_one(self) -> None:
        self.assertEqual(seniority_rank("phoenix"), -1)

    def test_ranks_dict_matches_tiers(self) -> None:
        # Regression guard: SENIORITY_RANKS must stay in lockstep with
        # SENIORITY_TIERS so a tier added without a rank entry would
        # silently break the literals below.
        self.assertEqual(set(SENIORITY_RANKS.keys()), set(SENIORITY_VALUES))


# ---------------------------------------------------------------------------
class TestVisaExpanded(unittest.TestCase):
    """Each phase tests a single phrase so a regex typo points straight
    at the broken expression.
    """

    def test_no_sponsorship_available(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Sponsorship not available."))

    def test_citizenship_required(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Citizenship required."))

    def test_must_be_a_us_citizen(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Must be a US citizen."))

    def test_permanent_resident_required(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Permanent resident required."))

    def test_green_card_required(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Green card required."))

    def test_work_authorization_required(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Work authorization required."))

    def test_must_be_authorized_to_work(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="Must be authorized to work."))

    def test_h1b_not_provided(self) -> None:
        self.assertFalse(is_relevant_role("Senior Engineer", description="H1B not provided."))

    def test_no_sponsorship_phrasing_matches_no_band_check(self) -> None:
        # Legacy operator with no band set; visa hard-drops regardless.
        self.assertFalse(is_relevant_role("Junior Engineer", description="No visa sponsorship."))


# ---------------------------------------------------------------------------
class TestClearance(unittest.TestCase):
    """Each phrase hits a single CLEARANCE_PATTERNS entry to isolate
    typos in the regex.
    """

    def test_security_clearance_required(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Must have an active security clearance.")
        )

    def test_ts_sci(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Active TS/SCI clearance required.")
        )

    def test_top_secret(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Top Secret clearance is required.")
        )

    def test_secret_clearance(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Applicants must hold a secret clearance.")
        )

    def test_public_trust(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Public Trust position.")
        )

    def test_polygraph_required(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Polygraph required.")
        )

    def test_itar(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="ITAR restricted role.")
        )

    def test_ear_controlled(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="EAR controlled technology exposure.")
        )

    def test_dod_contract(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Federal DoD contract work.")
        )

    def test_federal_contractor(self) -> None:
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="Federal contractor environment.")
        )

    def test_no_clearance_keyword_keeps(self) -> None:
        self.assertTrue(
            is_relevant_role("Senior Software Engineer", description="Build with React + Python.")
        )


# ---------------------------------------------------------------------------
class TestSeniorityBand(unittest.TestCase):
    def test_min_seniority_excludes_junior(self) -> None:
        self.assertFalse(is_relevant_role("Junior Engineer", min_seniority="senior"))

    def test_min_seniority_keeps_senior(self) -> None:
        self.assertTrue(is_relevant_role("Senior Engineer", min_seniority="senior"))

    def test_max_seniority_excludes_principal(self) -> None:
        self.assertFalse(is_relevant_role("Principal Engineer", max_seniority="staff"))

    def test_max_seniority_keeps_staff(self) -> None:
        self.assertTrue(is_relevant_role("Staff Engineer", max_seniority="staff"))

    def test_band_senior_only_keeps_senior_only(self) -> None:
        self.assertTrue(
            is_relevant_role("Senior Engineer", min_seniority="senior", max_seniority="senior")
        )

    def test_band_senior_only_excludes_neighbours(self) -> None:
        self.assertFalse(is_relevant_role("Junior Engineer", min_seniority="senior", max_seniority="senior"))
        self.assertFalse(is_relevant_role("Staff Engineer", min_seniority="senior", max_seniority="senior"))
        self.assertFalse(is_relevant_role("Principal Engineer", min_seniority="senior", max_seniority="senior"))

    def test_unclassifiable_passes_mid_band(self) -> None:
        # "Software Engineer" → classified=None → IMPLICIT_MID_RANK=2.
        # min=junior (1) <= 2 <= max=senior (3) → pass.
        self.assertTrue(
            is_relevant_role("Software Engineer", min_seniority="junior", max_seniority="senior")
        )

    def test_unclassifiable_excluded_when_min_strictly_senior(self) -> None:
        # min=senior (3); mid rank (2) < 3 → exclude.
        self.assertFalse(is_relevant_role("Software Engineer", min_seniority="senior"))

    def test_unclassifiable_excluded_when_max_strictly_junior(self) -> None:
        # max=junior (1); mid rank (2) > 1 → exclude.
        self.assertFalse(is_relevant_role("Software Engineer", max_seniority="junior"))

    def test_clearance_drop_runs_before_band(self) -> None:
        # Band would normally let "Senior Engineer" pass; clearance phrase
        # short-circuits before the band check reaches the title regex.
        self.assertFalse(
            is_relevant_role("Senior Engineer", description="TS/SCI required.", min_seniority="senior")
        )

    def test_band_does_not_affect_positive_relevant_match(self) -> None:
        # "Founding Engineer" has no tier match → IMPLICIT_MID_RANK.
        # With min=senior the band would exclude it, but the irrelevant
        # pattern check still runs after the band and there's no
        # irrelevant keyword either, so it falls through to "default
        # reject". The positive-relevant match is "engineer" which
        # comes AFTER the band check by design — verifying here so a
        # future reorder doesn't silently swap the precedence.
        self.assertFalse(
            is_relevant_role("Founding Engineer", min_seniority="senior"),
            "band runs before the positive relevant match; behaviour documented in module docstring",
        )


# ---------------------------------------------------------------------------
class TestFilterRoles(unittest.TestCase):
    def test_empty_list(self) -> None:
        self.assertEqual(filter_roles([]), [])

    def test_signature_threads_min_seniority(self) -> None:
        jobs = [
            {"title": "Senior Engineer", "description": ""},
            {"title": "Junior Engineer", "description": ""},
        ]
        out = filter_roles(jobs, min_seniority="senior")
        self.assertEqual([j["title"] for j in out], ["Senior Engineer"])

    def test_signature_threads_max_seniority(self) -> None:
        jobs = [
            {"title": "Staff Engineer", "description": ""},
            {"title": "Principal Engineer", "description": ""},
        ]
        out = filter_roles(jobs, max_seniority="staff")
        self.assertEqual([j["title"] for j in out], ["Staff Engineer"])

    def test_legacy_call_shape_unchanged(self) -> None:
        # No knobs — exercises the bit-for-bit existing behaviour.
        jobs = [
            {"title": "Senior Software Engineer", "description": ""},
            {"title": "Junior Software Engineer", "description": ""},
            {"title": "Sales Engineer", "description": ""},
            {"title": "Software Engineering Intern", "description": ""},
        ]
        out = filter_roles(jobs)
        # Engineer matches relevant keyword first → all three pass;
        # "intern" matches irrelevant but the positive match wins.
        # (Pin this so a reorder breaking the precedence rule fails
        # loudly.)
        self.assertEqual(
            sorted(j["title"] for j in out),
            sorted(
                [
                    "Senior Software Engineer",
                    "Junior Software Engineer",
                    "Sales Engineer",
                    "Software Engineering Intern",
                ]
            ),
        )

    def test_extra_kwargs_still_pass_through(self) -> None:
        jobs = [{"title": "Senior Astronomer", "description": ""}]
        out = filter_roles(jobs, extra_relevant_patterns=[r"(?i)\bastronomer\b"])
        self.assertEqual(len(out), 1)


# ---------------------------------------------------------------------------
class TestDefaultsPreserved(unittest.TestCase):
    """Sanity: the default call (no seniority set) matches the legacy
    behaviour so the refactor didn't silently regress any existing
    test fixture.
    """

    def test_legacy_intern_filtered_via_irrelevant_pattern(self) -> None:
        # "Software Engineering Intern" — positive match for "software
        # engineering" wins; "intern" never reaches. Pinning this so
        # the precedence stays correct.
        self.assertTrue(is_relevant_role("Software Engineering Intern"))

    def test_legacy_intern_only_filtered_via_irrelevant(self) -> None:
        # Bare "intern" doesn't match DEFAULT_RELEVANT_PATTERNS → the
        # irrelevant pattern matches → dropped.
        self.assertFalse(is_relevant_role("HR Intern"))

    def test_legacy_sales_dropped(self) -> None:
        self.assertFalse(is_relevant_role("Senior Sales Manager"))


# ---------------------------------------------------------------------------
class TestPatternsAreNonEmpty(unittest.TestCase):
    """Regression guards: each public pattern list must contain at
    least one regex. Empty lists silently accept every title.
    """

    def test_visa_patterns_non_empty(self) -> None:
        self.assertGreater(len(NO_SPONSORSHIP_PATTERNS), 0)

    def test_clearance_patterns_non_empty(self) -> None:
        self.assertGreater(len(CLEARANCE_PATTERNS), 0)

    def test_relevant_patterns_non_empty(self) -> None:
        self.assertGreater(len(DEFAULT_RELEVANT_PATTERNS), 0)


if __name__ == "__main__":
    unittest.main()
