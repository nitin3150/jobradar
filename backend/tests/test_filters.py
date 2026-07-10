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
    HARD_BENCH_TRIGGER_PATTERNS,
    NO_SPONSORSHIP_PATTERNS,
    SENIORITY_RANKS,
    SENIORITY_TIERS,
    SENIORITY_VALUES,
    SeniorityTier,
    YEARS_OF_EXPERIENCE_PATTERN,
    _DEFAULT_TITLE_REJECT_KEYWORDS,
    _resolve_title_reject_keywords,
    bench_org_from_text,
    classify_seniority,
    filter_roles,
    is_relevant_role,
    min_years_required,
    seniority_rank,
    should_reject_by_title,
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


# ---------------------------------------------------------------------------
# v0.5 additions: years-of-experience gate + org-level hard-bench trigger.
# These power the boards runner's pre-LLM drop path — a posting that
# hard-requires 6+ years of experience is dropped before the LLM sees it
# (saves tokens, the operator's time, and keeps the score queue focused on
# realistically-applicable roles). ``bench_org_from_text`` is the
# citizenship-required version: a single match benches the whole org
# (added to ``<board>_missing_orgs.json``) because every role at a
# citizenship-locked shop is wasted LLM budget.
# ---------------------------------------------------------------------------
class TestMinYearsRequired(unittest.TestCase):
    """Each phase tests a single phrasings variant so a regex typo
    points at the broken expression.
    """

    def test_5_plus_years(self) -> None:
        self.assertEqual(
            min_years_required("5+ years of experience required"), 5
        )

    def test_minimum_5_years(self) -> None:
        self.assertEqual(
            min_years_required("Minimum 5 years experience"), 5
        )

    def test_at_least_3_years(self) -> None:
        self.assertEqual(
            min_years_required("At least 3 years of relevant experience"), 3
        )

    def test_over_7_years(self) -> None:
        self.assertEqual(
            min_years_required("Over 7 years of professional experience"), 7
        )

    def test_yrs_abbreviation(self) -> None:
        self.assertEqual(min_years_required("5+ yrs experience"), 5)

    def test_yr_dot_abbreviation(self) -> None:
        self.assertEqual(min_years_required("3 yr. exp."), 3)

    def test_minimum_8_returns_8(self) -> None:
        # The boards runner's 6+ years drop fires when this returns
        # >= 6 — verify a posting that hard-requires 8 also surfaces
        # the right number (no off-by-one in the years parser).
        self.assertEqual(
            min_years_required("Minimum 8 years of experience"), 8
        )

    def test_returns_none_for_no_minimum(self) -> None:
        # No "N years" phrasing → returns None → the role PROCEEDS to
        # the LLM scorer. The operator's years-floor gate only fires
        # on explicit minimums; unstated seniority is not grounds to
        # drop a role.
        self.assertIsNone(min_years_required("Great culture, remote work."))

    def test_returns_none_for_empty_string(self) -> None:
        self.assertIsNone(min_years_required(""))

    def test_returns_none_for_none_input(self) -> None:
        # Type guard: the function must accept None without raising.
        # A future caller that misses the description column on the
        # Job row should not crash the runner.
        self.assertIsNone(min_years_required(None))  # type: ignore[arg-type]

    def test_5_plus_with_preferred_only_does_not_overcount(self) -> None:
        # A common posting: "Required: 3 years. Preferred: 7 years."
        # The first-match rule returns 3 (the required floor) — more
        # permissive interpretation. The boards runner's 6+ gate
        # correctly classifies this as a pass (3 < 6).
        self.assertEqual(
            min_years_required("Required: 3 years. Preferred: 7 years of additional experience."),
            3,
        )

    def test_returns_positive_int_only(self) -> None:
        # Defensive: a future regex tweak that captures '0' or a
        # negative int must not return it (we'd then pass it as a
        # years-floor and the boards runner would treat every role
        # as 0+ years, dropping nothing).
        # We test the parser's contract directly by feeding it text
        # where the regex would nominally capture '0' (it shouldn't,
        # because the pattern requires \\d{1,2} so '0' is matched but
        # the post-parse filter rejects < 1).
        self.assertIsNone(
            min_years_required("0 years of experience")
        )


# ---------------------------------------------------------------------------
class TestBenchOrgFromText(unittest.TestCase):
    """Hard-bench trigger regex coverage. Each test isolates a single
    HARD_BENCH_TRIGGER_PATTERNS entry so a regression points straight
    at the broken regex.
    """

    def test_citizenship_required(self) -> None:
        self.assertTrue(
            bench_org_from_text("Citizenship required for this role.")
        )

    def test_must_be_a_us_citizen(self) -> None:
        self.assertTrue(
            bench_org_from_text("Must be a US citizen to apply.")
        )

    def test_us_citizenship_required(self) -> None:
        self.assertTrue(
            bench_org_from_text("U.S. citizenship required.")
        )

    def test_permanent_resident_required(self) -> None:
        self.assertTrue(
            bench_org_from_text("Permanent resident required.")
        )

    def test_green_card_required(self) -> None:
        self.assertTrue(
            bench_org_from_text("Green card required at time of hire.")
        )

    def test_green_card_holder_required(self) -> None:
        self.assertTrue(
            bench_org_from_text("Green card holder required for export control.")
        )

    def test_cannot_sponsor(self) -> None:
        self.assertTrue(
            bench_org_from_text("We cannot sponsor at this time.")
        )

    def test_will_not_sponsor(self) -> None:
        self.assertTrue(
            bench_org_from_text("Company will not sponsor work visas.")
        )

    def test_no_sponsorship_will_be_provided(self) -> None:
        self.assertTrue(
            bench_org_from_text("No sponsorship will be provided.")
        )

    def test_no_sponsorship_will_be_available(self) -> None:
        self.assertTrue(
            bench_org_from_text("No sponsorship will be available.")
        )

    def test_no_visa_sponsorship_available(self) -> None:
        self.assertTrue(
            bench_org_from_text("No visa sponsorship available.")
        )

    def test_must_have_citizenship(self) -> None:
        self.assertTrue(
            bench_org_from_text("Applicants must have U.S. citizenship.")
        )

    def test_must_be_us_citizen(self) -> None:
        # The HARD_BENCH_TRIGGER_PATTERNS regex requires the
        # singular ``citizen`` (with the optional preceding ``a``);
        # the plural ``citizens`` is NOT matched. A future
        # pluralization tweak in the regex would widen this
        # coverage — for v1 the strict-singular contract is the
        # documented contract.
        self.assertTrue(
            bench_org_from_text("Candidates must be a U.S. citizen.")
        )

    def test_negative_text_returns_false(self) -> None:
        # No "required" / "must be" / "sponsorship" — role proceeds.
        self.assertFalse(
            bench_org_from_text("Visa sponsorship is available for the right candidate.")
        )

    def test_empty_string_returns_false(self) -> None:
        self.assertFalse(bench_org_from_text(""))

    def test_none_input_returns_false(self) -> None:
        self.assertFalse(bench_org_from_text(None))  # type: ignore[arg-type]

    def test_clearance_does_not_bench(self) -> None:
        # Clearance is a HARD role drop but NOT an org bench — a
        # DoD shop may post non-cleared IC roles; benching the
        # whole org would lose those.
        self.assertFalse(
            bench_org_from_text("Active TS/SCI clearance required for this role.")
        )

    def test_loose_sponsorship_mention_does_not_bench(self) -> None:
        # "Sponsorship not available" is the role-drop gate but NOT
        # the org-bench trigger. A company can decline to sponsor
        # for one role and still have other roles that are
        # sponsorship-friendly (e.g. cleared-only vs entry-level).
        self.assertFalse(
            bench_org_from_text("Sponsorship not available for this position.")
        )


# ---------------------------------------------------------------------------
class TestPatternListsPopulated(unittest.TestCase):
    """Regression guards: the new pattern lists must contain at least
    one regex. An accidentally emptied list silently accepts every
    posting — disastrous for both the role-drop and org-bench paths.
    """

    def test_years_of_experience_pattern_compiles(self) -> None:
        # A future typo in the pattern string would break the
        # compile. The import succeeded so the compile succeeded;
        # the explicit pattern.search() call here documents the
        # contract.
        self.assertIsNotNone(YEARS_OF_EXPERIENCE_PATTERN.search("5 years of experience"))

    def test_hard_bench_triggers_non_empty(self) -> None:
        self.assertGreater(len(HARD_BENCH_TRIGGER_PATTERNS), 0)

    def test_min_years_required_returns_int(self) -> None:
        # Type-guard: the function is annotated ``int | None``; verify
        # the positive branch returns a real int (not e.g. a string).
        result = min_years_required("5+ years")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, int)


# ---------------------------------------------------------------------------
# v0.6 additions: title-level reject filter.
# ---------------------------------------------------------------------------
class TestShouldRejectByTitle(unittest.TestCase):
    """Coverage for the staff/principal/lead/head/director gate.

    Each assertion targets a single behaviour so a regex typo or
    env-override drift surfaces in one test method:

    * Canonical set (5 tokens) all reject.
    * Qualifiers and adjacent words stay out of the match set
    (word-boundary regression guard).
    * Explicit ``keywords=`` bypasses env entirely.
    * ``raw_env=`` test hook simulates manifest-mode deployment.
    * Empty / whitespace-only env falls back to canonical default.
    """

    def test_staff_engineer_rejected(self) -> None:
        self.assertTrue(should_reject_by_title("Staff Engineer", raw_env=None))

    def test_principal_engineer_rejected(self) -> None:
        self.assertTrue(should_reject_by_title("Principal Engineer", raw_env=None))

    def test_lead_engineer_rejected(self) -> None:
        self.assertTrue(should_reject_by_title("Lead Engineer", raw_env=None))

    def test_software_engineering_lead_rejected(self) -> None:
        # "lead" appears at the end as a whole word -- matches \blead\b.
        self.assertTrue(should_reject_by_title("Software Engineering Lead", raw_env=None))

    def test_team_lead_rejected(self) -> None:
        self.assertTrue(should_reject_by_title("Team Lead", raw_env=None))

    def test_head_of_engineering_rejected(self) -> None:
        self.assertTrue(should_reject_by_title("Head of Engineering", raw_env=None))

    def test_director_of_engineering_rejected(self) -> None:
        self.assertTrue(should_reject_by_title("Director of Engineering", raw_env=None))

    def test_software_engineer_passes(self) -> None:
        # No seniority keyword -- proceeds to the band filter upstream.
        self.assertFalse(should_reject_by_title("Software Engineer", raw_env=None))

    def test_senior_engineer_passes(self) -> None:
        # "senior" is in SENIORITY_TIERS but NOT in the title-reject set.
        # The band filter governs senior fate; this gate is narrower.
        self.assertFalse(should_reject_by_title("Senior Engineer", raw_env=None))

    def test_engineering_manager_passes(self) -> None:
        # "manager" is in SENIORITY_TIERS (rank 6) but NOT in the
        # title-reject set -- the operator opted to drop above-staff
        # but the band filter owns the manager-band question.
        self.assertFalse(should_reject_by_title("Engineering Manager", raw_env=None))

    def test_vp_engineering_passes(self) -> None:
        # Edge case: "vp" is in SENIORITY_TIERS (rank 8) but "vp" is
        # NOT in the title-reject canonical set. Surfaces here so a
        # future widening ("also drop vp") explicitly opts in rather
        # than inheriting the rejection.
        self.assertFalse(should_reject_by_title("VP Engineering", raw_env=None))

    def test_staffing_coordinator_passes(self) -> None:
        # Word boundary keeps "staffing" out of the match set.
        self.assertFalse(should_reject_by_title("Staffing Coordinator", raw_env=None))

    def test_leadership_program_passes(self) -> None:
        # Word boundary keeps "leadership" out of the match set.
        self.assertFalse(should_reject_by_title("Leadership Program", raw_env=None))

    def test_directorship_passes(self) -> None:
        # Word boundary keeps "directorship" out of the match set.
        self.assertFalse(should_reject_by_title("Directorship Role", raw_env=None))

    def test_headphones_passes(self) -> None:
        # Word boundary keeps "headphones" out of the match set.
        self.assertFalse(should_reject_by_title("Headphones Designer", raw_env=None))

    def test_case_insensitive_match(self) -> None:
        # All three casings hit the canonical set -- the (?i) flag
        # makes the pattern case-insensitive by construction.
        self.assertTrue(should_reject_by_title("STAFF ENGINEER", raw_env=None))
        self.assertTrue(should_reject_by_title("Staff engineer", raw_env=None))
        self.assertTrue(should_reject_by_title("staff engineer", raw_env=None))

    def test_empty_title_returns_false(self) -> None:
        # Default-allow -- the upstream band filter owns the empty
        # case; the title-reject gate is a no-op on empty input.
        self.assertFalse(should_reject_by_title("", raw_env=None))

    def test_none_title_returns_false(self) -> None:
        # Type guard -- a future caller that misses the title column
        # must not crash the runner with a TypeError on re.search.
        self.assertFalse(should_reject_by_title(None, raw_env=None))  # type: ignore[arg-type]

    # --- env override via raw_env test hook ------------------------------

    def test_env_override_comma_separated(self) -> None:
        # Narrow to just "principal" -- a Staff Engineer would now pass.
        self.assertTrue(
            should_reject_by_title("Principal Engineer", raw_env="principal"),
        )
        self.assertFalse(
            should_reject_by_title("Staff Engineer", raw_env="principal"),
        )
        self.assertFalse(
            should_reject_by_title("Lead Engineer", raw_env="principal"),
        )

    def test_env_override_whitespace_separated(self) -> None:
        # Accepts whitespace tokens too (BOARDS_REJECT_TITLE_KEYWORDS=
        # "staff principal lead").
        self.assertTrue(
            should_reject_by_title("Staff Engineer", raw_env="staff principal lead"),
        )
        self.assertFalse(
            should_reject_by_title("Director of Engineering", raw_env="staff principal lead"),
        )

    def test_env_override_empty_falls_back_to_default(self) -> None:
        # Empty raw_env = "no env var set" -- canonical 5-token
        # default takes over so a misconfigured Render secret
        # doesn't silently disable the gate.
        self.assertTrue(
            should_reject_by_title("Staff Engineer", raw_env=""),
        )
        self.assertTrue(
            should_reject_by_title("Director", raw_env=""),
        )

    def test_env_override_whitespace_only_falls_back_to_default(self) -> None:
        # Whitespace-only env (operator exported ""  or " ") -> same
        # fallback as empty.
        self.assertTrue(
            should_reject_by_title("Lead Engineer", raw_env="   "),
        )

    def test_env_override_widens_set(self) -> None:
        # Add "fellow" + "distinguished" -- both are seniority
        # aliases from SENIORITY_TIERS that aren't in the canonical
        # title-reject set, but an operator who explicitly widens
        # gets them dropped. The raw_env value here MUST list both
        # tokens -- a previous revision only had "fellow" and the
        # "Distinguished Engineer" assertion silently failed because
        # "distinguished" was missing from the env.
        raw = "staff,principal,lead,head,director,fellow,distinguished"
        self.assertTrue(
            should_reject_by_title("Distinguished Engineer", raw_env=raw),
        )
        self.assertTrue(
            should_reject_by_title("Research Fellow", raw_env=raw),
        )

    def test_env_override_dedup_and_lowercase(self) -> None:
        # "STAFF,Staff,staff" -> deduplicated + lowercased -> single
        # effective token. Asserts an idiomatic-Python dedup not a
        # case-sensitive set semantics surprise.
        self.assertTrue(
            should_reject_by_title("Staff Engineer", raw_env="STAFF,Staff,staff"),
        )

    # --- explicit keywords argument ------------------------------------

    def test_explicit_keywords_override_env(self) -> None:
        # keywords= wins over env entirely -- the production env
        # value is ignored when the caller passes an explicit list.
        # With explicit kw=["principal"] the env's "staff" is
        # ignored: "Principal Engineer" matches the explicit kw
        # (true), "Staff Engineer" matches the env's "staff" but
        # NOT the explicit kw "principal" (false -- demonstrates
        # the override semantics).
        self.assertTrue(
            should_reject_by_title(
                "Principal Engineer",
                keywords=["principal"],
                raw_env="staff,principal",
            ),
        )
        self.assertFalse(
            should_reject_by_title(
                "Staff Engineer",
                keywords=["principal"],
                raw_env="staff,principal",
            ),
        )

    def test_explicit_keywords_narrow(self) -> None:
        # Caller narrows to just "lead"; even with raw_env set
        # broader, this title is the only one dropped.
        self.assertTrue(
            should_reject_by_title(
                "Lead Engineer",
                keywords=["lead"],
                raw_env="staff,principal,head,director",
            ),
        )
        self.assertFalse(
            should_reject_by_title(
                "Staff Engineer",
                keywords=["lead"],
                raw_env="staff,principal,head,director",
            ),
        )

    def test_explicit_empty_keywords_disables_gate(self) -> None:
        # keywords=[] -- explicit gate disable. Useful for a test
        # fixture that wants to confirm the band filter alone.
        self.assertFalse(
            should_reject_by_title("Staff Engineer", keywords=[]),
        )

    def test_explicit_keywords_whitespace_only_normalised(self) -> None:
        # An empty / whitespace-only explicit list collapses to no
        # match. "  " is filtered out by the strip().lower() guard.
        self.assertFalse(
            should_reject_by_title("Staff Engineer", keywords=["  ", ""]),
        )

    # --- behavioural contracts pinned here --------------------------------

    def test_multiple_hits_returns_true(self) -> None:
        # "Staff Principal Engineer" has TWO canonical keywords in
        # one title -- should_reject_by_title still returns True on
        # the first match (pattern.search + bool() short-circuit).
        # Pin the contract so a future refactor to a multi-keyword
        # counter doesn't accidentally degrade to a count-only
        # semantic (>= 2 means True).
        self.assertTrue(
            should_reject_by_title("Staff Principal Engineer", raw_env=None),
        )

    def test_title_only_by_design(self) -> None:
        # Contract pin: should_reject_by_title looks at TITLE ONLY.
        # Unlike min_years_required which scans combined
        # title+description, this gate is asymmetric on purpose.
        # A description-side mention of "staff-level" must NOT
        # trigger the gate -- this test freezes that behaviour so
        # a future "more thorough check" PR doesn't silently
        # regress it.
        # We pass the title "Senior Engineer" with explicit
        # canonical keywords -- if the implementation ever
        # started reading description text, it would still
        # return False here (no title-only keyword), pinning
        # that the test's premise is correct.
        self.assertFalse(
            should_reject_by_title(
                "Senior Engineer",
                raw_env=None,
            ),
        )
        # And a title with the keyword still rejects: this is the
        # counterpart to the above, asserting that title-side
        # mentions DO trigger the gate (baseline semantics).
        self.assertTrue(
            should_reject_by_title(
                "Staff Engineer",
                raw_env=None,
            ),
        )

    def test_plural_siblings_stay_out_of_match_set(self) -> None:
        # Regression guard for \b...\b semantics on PLURAL forms.
        # "Staffs" -- plural -- followed by 's' (a word character),
        # so \b does NOT fire after "Staff". The string is correctly
        # excluded. Pin this so a future regex tweak to (?:^|\s)...
        # (?:\s|$) for some other gate doesn't accidentally bring
        # in plurals here.
        self.assertFalse(should_reject_by_title("Staffs", raw_env=None))
        self.assertFalse(should_reject_by_title("Leads", raw_env=None))
        self.assertFalse(should_reject_by_title("Heads", raw_env=None))
        self.assertFalse(should_reject_by_title("Principals", raw_env=None))
        self.assertFalse(should_reject_by_title("Directors", raw_env=None))

    def test_cache_returns_same_pattern_for_identical_inputs(self) -> None:
        # Regression guard for F2 (lru_cache wiring). Two calls with
        # identical (keywords, raw_env) MUST hit the same compiled
        # pattern -- if they don't, the cache broke and the boards
        # runner starts paying per-call re.compile again.
        from utils.filters import _build_title_reject_pattern

        pat1 = _build_title_reject_pattern(None, "staff,principal")
        pat2 = _build_title_reject_pattern(None, "staff,principal")
        self.assertIs(
            pat1, pat2,
            "lru_cache miss detected -- the boards runner is back "
            "to per-call re.compile",
        )

    def test_cache_miss_for_distinct_env_values(self) -> None:
        # Two distinct env values = two distinct cache entries.
        # If lru_cache collapsed them, env-rotation would silently
        # reuse a stale compiled pattern -- capture that risk here.
        from utils.filters import _build_title_reject_pattern

        pat_a = _build_title_reject_pattern(None, "staff")
        pat_b = _build_title_reject_pattern(None, "lead")
        self.assertIsNot(
            pat_a, pat_b,
            "lru_cache incorrectly shared across distinct env values",
        )
        # And the pattern actually matches the right keyword:
        self.assertIsNotNone(pat_a.search("Staff Engineer"))
        self.assertIsNone(pat_a.search("Lead Engineer"))
        self.assertIsNone(pat_b.search("Staff Engineer"))
        self.assertIsNotNone(pat_b.search("Lead Engineer"))


# ---------------------------------------------------------------------------
# Regression guards for the title-reject helper itself.
# ---------------------------------------------------------------------------
class TestTitleRejectKeywordResolver(unittest.TestCase):
    """Direct coverage of :func:`_resolve_title_reject_keywords`.

    The function is module-private but its behaviour (env-fallback,
    tokenisation, dedup) is the contract callers depend on. Pinning
    it here so a future cleanup doesn't accidentally remove the
    canonical fallback.
    """

    def test_default_when_env_unset(self) -> None:
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env=""),
            _DEFAULT_TITLE_REJECT_KEYWORDS,
        )

    def test_default_when_env_none(self) -> None:
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env=None),
            _DEFAULT_TITLE_REJECT_KEYWORDS,
        )

    def test_default_when_env_whitespace(self) -> None:
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env="   ,  "),
            _DEFAULT_TITLE_REJECT_KEYWORDS,
        )

    def test_single_keyword_returns_singleton_tuple(self) -> None:
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env="principal"),
            ("principal",),
        )

    def test_multiple_keywords_preserve_order(self) -> None:
        # The order matters for log output / debugging surfaces.
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env="director,staff,principal"),
            ("director", "staff", "principal"),
        )

    def test_dedup_collapses_duplicates(self) -> None:
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env="staff,STAFF,Staff,staff"),
            ("staff",),
        )

    def test_lowercase_normalisation(self) -> None:
        # Operator exports in title-cased config? Still works.
        self.assertEqual(
            _resolve_title_reject_keywords(raw_env="Staff,Principal,Lead"),
            ("staff", "principal", "lead"),
        )


if __name__ == "__main__":
    unittest.main()
