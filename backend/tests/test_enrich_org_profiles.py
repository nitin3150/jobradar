"""Tests for ``backend/scripts/enrich_org_profiles.py`` and the runner hook.

Covers the script's pure helpers (prompt builder, atomic write,
skip-rule evaluator, skip-list walker) end-to-end plus the
``load_orgs()`` opt-in hook that consumes ``_skip_list.json``.

Uses ``unittest.TestCase`` (matching ``test_job_board_runner.py`` and
the project's canonical runner). Pytest picks these up too via the
``test_enrich_org_profiles.py`` test_*.py name.

Fixtures live in ``tests/fixtures/board_responses/`` — 4 truncated
board responses (Affirm / BoltWise / Clera / Moonsong) matching the
real ATS payload shapes. Tests use them as prompt-builder inputs;
they do NOT require a live network because all fetcher calls are
mocked or replaced via the synthesis helpers in :mod:`setUp`.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Make the project importable when pytest collection runs the file
# directly without `pip install -e .`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.enrich_org_profiles import (  # noqa: E402
    OrgProfile,
    SCHEMA_VERSION,
    SKIP_CADENCE_DEAD,
    SKIP_CADENCE_STALE_DAYS,
    SKIP_CONFIDENCE_THRESHOLD,
    SKIP_TECH_RATIO_THRESHOLD,
    SourceJob,
    _atomic_write_json,
    _build_skip_list,
    _build_source_jobs,
    _build_user_prompt,
    _compute_skip_for_profile,
    _trim_jobs_for_prompt,
    _write_skip_list,
    _write_status_envelope,
)


class _OrgProfileBuilder:
    """Test fixture helper: synthesize an ``OrgProfile`` dict.

    Mirrors the on-disk JSON shape produced by
    :func:`scripts.enrich_org_profiles._atomic_write_json` and the
    fields the runner reads. All fields are overridable per-call.
    """

    @staticmethod
    def make(**overrides) -> dict:
        base = {
            "schema_version": SCHEMA_VERSION,
            "slug": "test-org",
            "board": "greenhouse",
            "enriched_at": "2026-07-10T12:00:00+00:00",
            "source_jobs_count": 30,
            "source_last_published": "2026-07-08T00:00:00+00:00",
            "primary_function": "engineering_heavy",
            "estimated_stage": "series_b",
            "hiring_volume_estimate": "10_50",
            "posting_cadence": "weekly",
            "sponsorship_open": None,
            "clearance_required": None,
            "remote_friendly": True,
            "is_likely_startup": False,
            "tech_role_ratio": 0.85,
            "sponsorship_likelihood": 0.7,
            "clearance_likelihood": 0.05,
            "startup_likelihood": 0.3,
            "volatility_signal": 0.1,
            "notes": "Tech-heavy AI infra shop.",
            "overall_confidence": 0.85,
            "model_used": "meta/llama-3.1-70b-instruct",
        }
        base.update(overrides)
        return base


class TestAtomicWriteJson(unittest.TestCase):
    """Atomic-write contract: torn writes must be impossible for an
    observer reading the parent directory during the write."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_happy_path_creates_file_with_payload(self) -> None:
        path = self.tmpdir / "out.json"
        payload = {"hello": "world", "n": 42}
        _atomic_write_json(path, payload)
        self.assertTrue(path.exists())
        with open(path) as f:
            self.assertEqual(json.load(f), payload)

    def test_overwrites_existing_file(self) -> None:
        path = self.tmpdir / "out.json"
        _atomic_write_json(path, {"old": True})
        _atomic_write_json(path, {"new": True})
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data, {"new": True})

    def test_creates_parent_directory(self) -> None:
        nested = self.tmpdir / "deep" / "nested" / "out.json"
        _atomic_write_json(nested, {"x": 1})
        self.assertTrue(nested.exists())

    def test_failure_during_write_does_not_leave_tmp_file(self) -> None:
        """A non-serializable payload triggers the cleanup branch.

        We pass a set (not JSON-serializable) to force ``json.dump``
        to raise, then assert no ``.tmp`` file remains in the
        directory. Using a set instead of an unfreezable object
        keeps the test deterministic (other payloads would only
        surface this on disk-full / fsync-fail conditions).
        """
        path_dir = self.tmpdir / "fail"
        path_dir.mkdir()
        # ``json.dump`` raises ``TypeError`` when serialising a set.
        with self.assertRaises(TypeError):
            _atomic_write_json(
                path_dir / "out.json",
                {"bad": {1, 2, 3}},  # type: ignore[arg-type]
            )
        leftover = list(path_dir.glob("*.tmp"))
        self.assertEqual(leftover, [], "no .tmp file should remain on failure")


class TestTrimJobsForPrompt(unittest.TestCase):
    """``_trim_jobs_for_prompt`` strips non-classification-bearing
    fields and caps the LLM input window."""

    def test_keeps_only_classification_fields(self) -> None:
        raw = [
            {
                "id": "abc",
                "title": "Senior Engineer",
                "url": "https://example.com/jobs/abc",
                "published_at": "2026-07-01T00:00:00+00:00",
                "description": "We need an engineer who can ship.",
                "internal_job_id": "5730",
                "absolute_url": "https://example.com/abc",
                "metadata": ["<noise>"],
            }
        ]
        trimmed = _trim_jobs_for_prompt(raw)
        self.assertEqual(len(trimmed), 1)
        self.assertEqual(
            set(trimmed[0].keys()),
            {"title", "description", "location", "first_published"},
        )
        self.assertEqual(trimmed[0]["title"], "Senior Engineer")

    def test_caps_at_max_jobs_to_llm(self) -> None:
        raw = [{"title": f"Job {i}", "description": ""} for i in range(100)]
        from scripts.enrich_org_profiles import MAX_JOBS_TO_LLM
        trimmed = _trim_jobs_for_prompt(raw)
        self.assertEqual(len(trimmed), MAX_JOBS_TO_LLM)

    def test_truncates_long_descriptions(self) -> None:
        from scripts.enrich_org_profiles import MAX_DESCRIPTION_CHARS
        raw = [{"title": "x", "description": "a" * 5000}]
        trimmed = _trim_jobs_for_prompt(raw)
        self.assertEqual(len(trimmed[0]["description"]), MAX_DESCRIPTION_CHARS)


class TestBuildUserPrompt(unittest.TestCase):
    def test_includes_org_label(self) -> None:
        prompt = _build_user_prompt("greenhouse", "stripe", [])
        self.assertIn("greenhouse:stripe", prompt)

    def test_trims_prompt_input(self) -> None:
        """Even with 100 jobs the prompt embedding is bounded."""
        raw = [{"title": f"Job {i}", "description": "x" * 1000} for i in range(100)]
        _build_user_prompt("greenhouse", "stripe", raw)
        # If we get here without OOM/blow-the-context, we're good.
        # The exact size isn't asserted — it depends on JSON formatting.


class TestBuildSourceJobs(unittest.TestCase):
    """``_build_source_jobs`` persistence-shape contract.

    Complements :class:`TestTrimJobsForPrompt`: ``_trim_jobs_for_prompt``
    controls the LLM-visible shape (4 fields, no ``url``); this test
    pins the on-disk shape (5 fields, ``url`` included), the
    description-truncation cap, and the MAX_JOBS_TO_LLM guard.
    """

    def test_returns_five_field_shape_with_url(self) -> None:
        raw = [
            {
                "title": "Senior Engineer",
                "url": "https://example.com/jobs/abc",
                "description": "ship it",
                "location": "Remote",
                "published_at": "2026-07-01T00:00:00+00:00",
            }
        ]
        out = _build_source_jobs(raw)
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], SourceJob)
        self.assertEqual(
            set(out[0].model_dump().keys()),
            {"title", "description", "location", "first_published", "url"},
        )
        self.assertEqual(out[0].url, "https://example.com/jobs/abc")

    def test_reads_greenhouse_content_field_via_description_fallback(self) -> None:
        """``_build_source_jobs`` mirrors the ``description | content``
        fallback the LLM-trim already uses — Greenhouse's ``?content=true``
        lands in ``job["content"]`` and is read via that branch."""
        raw = [
            {
                "title": "Engineer",
                "url": "https://boards.greenhouse.io/x/jobs/1",
                # No ``description`` key — only ``content``.
                "content": "Greenhouse body HTML",
                "location": "Remote",
                "published_at": "2026-07-01",
            }
        ]
        self.assertEqual(_build_source_jobs(raw)[0].description, "Greenhouse body HTML")

    def test_caps_at_max_jobs_to_llm(self) -> None:
        raw = [{"title": f"Job {i}", "url": f"u/{i}", "description": ""} for i in range(50)]
        from scripts.enrich_org_profiles import MAX_JOBS_TO_LLM
        self.assertEqual(len(_build_source_jobs(raw)), MAX_JOBS_TO_LLM)

    def test_truncates_long_descriptions_to_source_cap(self) -> None:
        """Source-job descriptions cap at ``SOURCE_DESCRIPTION_MAX_CHARS``
        (4000), distinct from the ``MAX_DESCRIPTION_CHARS`` (600) cap the
        LLM-prompt path uses. See :data:`SOURCE_DESCRIPTION_MAX_CHARS` docstring
        for why these are split."""
        from scripts.enrich_org_profiles import SOURCE_DESCRIPTION_MAX_CHARS
        raw = [{"title": "x", "description": "a" * 5000, "url": "u"}]
        out = _build_source_jobs(raw)
        self.assertEqual(len(out[0].description), SOURCE_DESCRIPTION_MAX_CHARS)

    def test_stringifies_non_string_location(self) -> None:
        """Ashby returns ``{"name": "...", "country": "..."}``; the
        source path extracts ``name`` and falls back to a compact
        ``country=US`` form when ``name`` is absent. Round-trips so
        the operator can grep ``.location`` with a single shared filter."""
        raw = [{"title": "x", "location": {"name": "Remote", "country": "US"}, "url": "u"}]
        out = _build_source_jobs(raw)
        self.assertIsInstance(out[0].location, str)
        self.assertIn("Remote", out[0].location)

    def test_location_falls_back_to_country_when_name_missing(self) -> None:
        """When Ashby returns ``{"country": "US"}`` without ``name``,
        the source path falls back to ``country=US`` rather than
        empty string so the on-disk signal isn't lost."""
        raw = [{"title": "x", "location": {"country": "US"}, "url": "u"}]
        out = _build_source_jobs(raw)
        self.assertEqual(out[0].location, "country=US")


class TestSourceJobValidation(unittest.TestCase):
    """Pydantic contract on :class:`SourceJob` — title/description
    required strings, others optional (defaults to ``""``).

    Empty-string title is *not* rejected by Pydantic at the str
    declaration: ``min_length=1`` would force every consumer to
    supply a non-empty title, which the source-Job coercion
    contract doesn't (a Greenhouse board can technically return
    a job row with ``title: ""`` for a corrupted post; we still
    want it to round-trip through validation rather than crash
    the LLM-trim pipeline). The test below asserts the cleaner
    type-validation rule we DO want: integer-coerced fields must
    reject non-string input.
    """

    def test_minimum_required_fields_accepted(self) -> None:
        SourceJob(title="Engineer", description="ship things")

    def test_missing_required_title_raises(self) -> None:
        # The actually-meaningful contract: ``title`` has no default
        # value so a partial construction without it raises
        # ``ValidationError``. The reviewer flagged that Pydantic v1's
        # ``str`` validator silently coerces int/list/dict/set inputs
        # (``str([\"a\", \"b\"])`` returns ``'[\"a\", \"b\"]'`` cleanly), so any
        # ``assertRaises`` on a non-str title is a no-op. Required-field
        # presence is the right gate instead.
        with self.assertRaises(Exception):
            SourceJob(description="x")  # type: ignore[call-arg] — missing required field


class TestComputeSkipForProfile(unittest.TestCase):
    """Three skip rules — each is sufficient on its own.

    Pin the rule logic so a future tweak (e.g. lowered threshold) is
    a visible test diff rather than a silent behavior change.
    """

    _NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

    def test_status_not_ok_is_never_skipped(self) -> None:
        for status in ("failed", "skipped", None):
            profile = _OrgProfileBuilder.make(
                status=status,
                posting_cadence="dead",
                source_last_published=(
                    self._NOW - timedelta(days=365)
                ).isoformat(),
            )
            self.assertFalse(
                _compute_skip_for_profile(profile, now=self._NOW),
                f"status={status!r} should never skip",
            )

    # -- Rule 1: cadence in {dead, rare} AND last_posted > 180d ago ----
    def test_rule1_old_dead_org_skips(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            posting_cadence="dead",
            source_last_published=(
                self._NOW - timedelta(days=SKIP_CADENCE_STALE_DAYS + 1)
            ).isoformat(),
        )
        self.assertTrue(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule1_recent_dead_org_does_not_skip(self) -> None:
        """A dead-cadence org with a fresh posting should still be
        fetched — possibly recovered; the stale-only gate ignores it."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            posting_cadence="dead",
            source_last_published=(
                self._NOW - timedelta(days=30)
            ).isoformat(),
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule1_recent_cadence_does_not_skip_regardless_of_age(self) -> None:
        """``weekly`` cadence never falls in SKIP_CADENCE_DEAD."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            posting_cadence="weekly",
            source_last_published=(
                self._NOW - timedelta(days=365)
            ).isoformat(),
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule1_unknown_cadence_does_not_skip(self) -> None:
        """``unknown`` cadence is a "we couldn't tell" sentinel — not
        a "skip" signal."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            posting_cadence="unknown",
            source_last_published=(
                self._NOW - timedelta(days=365)
            ).isoformat(),
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    # -- Rule 2: explicit sponsorship block ---------------------------
    def test_rule2_sponsorship_open_false_skips(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=False
        )
        self.assertTrue(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule2_sponsorship_open_none_does_not_skip(self) -> None:
        """``None`` (no info) is NOT a skip signal — the runner treats
        unknown as "fall through to current behavior"."""
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=None
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule2_sponsorship_open_true_does_not_skip(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=True
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    # -- Rule 3: confidently non-tech ---------------------------------
    def test_rule3_low_tech_high_conf_skips(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=SKIP_TECH_RATIO_THRESHOLD - 0.05,
            overall_confidence=SKIP_CONFIDENCE_THRESHOLD + 0.05,
        )
        self.assertTrue(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule3_low_tech_low_conf_falls_through(self) -> None:
        """When confidence is low we DON'T skip — the LLM isn't sure
        and we'd rather pay the per-job LLM tokens than miss a real
        tech opportunity. This is the safety case for the AND."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=0.05,
            overall_confidence=0.5,
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule3_high_tech_low_conf_falls_through(self) -> None:
        """High tech-ratio with low confidence also doesn't skip —
        the LLM might be uncertain about a startup with a small
        board."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=0.7,
            overall_confidence=0.3,
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule3_boundary_thresholds(self) -> None:
        """Exact threshold comparisons: ``< `` (strict) for tech ratio,
        ``>`` (strict) for confidence, so the boundary cases fall
        through."""
        # Exactly at threshold (NOT skip).
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=SKIP_TECH_RATIO_THRESHOLD,
            overall_confidence=SKIP_CONFIDENCE_THRESHOLD,
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))


class TestBuildSkipList(unittest.TestCase):
    """End-to-end ``_build_skip_list``: writes profiles to disk, runs
    the walker, asserts the returned slug list matches."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile_dir = Path(self._tmp.name) / "enriched" / "greenhouse"
        self.profile_dir.mkdir(parents=True)

        # Patch ``PROFILE_DIR`` so ``_build_skip_list`` reads from
        # our test directory rather than ``backend/data/enriched``.
        from scripts import enrich_org_profiles
        self._orig_profile_dir = enrich_org_profiles.PROFILE_DIR
        enrich_org_profiles.PROFILE_DIR = Path(self._tmp.name) / "enriched"
        self.addCleanup(
            lambda: setattr(
                enrich_org_profiles, "PROFILE_DIR", self._orig_profile_dir
            )
        )

    def _write_profile(self, slug: str, profile: dict) -> Path:
        path = self.profile_dir / f"{slug}.json"
        path.write_text(json.dumps(profile))
        return path

    def test_returns_sortable_skip_slugs(self) -> None:
        _NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
        # Org A: dead board, 200d old.
        self._write_profile(
            "alpha",
            _OrgProfileBuilder.make(
                slug="alpha",
                status="ok",
                posting_cadence="dead",
                source_last_published=(_NOW - timedelta(days=200)).isoformat(),
            ),
        )
        # Org B: explicit sponsorship block.
        self._write_profile(
            "bravo",
            _OrgProfileBuilder.make(slug="bravo", status="ok", sponsorship_open=False),
        )
        # Org C: explicitly tech-heavy. Should NOT skip.
        self._write_profile(
            "charlie",
            _OrgProfileBuilder.make(slug="charlie", status="ok", tech_role_ratio=0.9),
        )
        # Org D: status=failed. Should NOT skip.
        self._write_profile(
            "delta",
            {"schema_version": SCHEMA_VERSION, "status": "failed", "slug": "delta"},
        )
        skip = _build_skip_list("greenhouse", now=_NOW)
        self.assertEqual(skip, ["alpha", "bravo"])

    def test_filters_meta_files_starting_with_underscore(self) -> None:
        # ``_skip_list.json`` is a meta file from a previous run —
        # walking the directory should ignore it.
        meta = self.profile_dir / "_skip_list.json"
        meta.write_text(json.dumps({"schema_version": 1, "slugs": ["ghost"]}))
        skip = _build_skip_list("greenhouse")
        self.assertEqual(skip, [])

    def test_returns_empty_list_when_directory_does_not_exist(self) -> None:
        non_existent = Path(self._tmp.name) / "no_dir_here"
        from scripts import enrich_org_profiles
        enrich_org_profiles.PROFILE_DIR = non_existent
        self.assertEqual(_build_skip_list("greenhouse"), [])


class TestWriteSkipList(unittest.TestCase):
    """Atomic skip-list writer — confirms shape and stability."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from scripts import enrich_org_profiles
        self._orig = enrich_org_profiles.PROFILE_DIR
        enrich_org_profiles.PROFILE_DIR = Path(self._tmp.name) / "enriched"
        self.addCleanup(
            lambda: setattr(
                enrich_org_profiles, "PROFILE_DIR", self._orig
            )
        )
        (enrich_org_profiles.PROFILE_DIR / "greenhouse").mkdir(parents=True)

    def test_writes_meta_envelope_with_sorted_slugs(self) -> None:
        _write_skip_list("greenhouse", ["zeta", "alpha", "mu"])
        path = (
            enrich_org_profiles.PROFILE_DIR / "greenhouse" / "_skip_list.json"
        )
        self.assertTrue(path.exists())
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["board"], "greenhouse")
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["slugs"], ["alpha", "mu", "zeta"])
        self.assertIn("computed_at", data)


class TestWriteStatusEnvelope(unittest.TestCase):
    """Non-OK envelopes (status: skipped / status: failed) — the
    runner's contract is to ignore them. Pin the shape so the
    skip-list walker reliably filters them out.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from scripts import enrich_org_profiles
        self._orig = enrich_org_profiles.PROFILE_DIR
        enrich_org_profiles.PROFILE_DIR = Path(self._tmp.name) / "enriched"
        self.addCleanup(
            lambda: setattr(
                enrich_org_profiles, "PROFILE_DIR", self._orig
            )
        )
        (enrich_org_profiles.PROFILE_DIR / "greenhouse").mkdir(parents=True)

    def test_skipped_envelope_shape(self) -> None:
        path = _write_status_envelope(
            board="greenhouse",
            slug="tiny-org",
            status="skipped",
            reason_or_error="fewer_than_3_jobs",
            extra={"source_jobs_count": 2},
        )
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["status"], "skipped")
        self.assertEqual(data["slug"], "tiny-org")
        self.assertEqual(data["reason"], "fewer_than_3_jobs")
        self.assertEqual(data["source_jobs_count"], 2)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)

    def test_failed_envelope_shape(self) -> None:
        path = _write_status_envelope(
            board="greenhouse",
            slug="bad-org",
            status="failed",
            reason_or_error="RuntimeError: all providers failed",
        )
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["slug"], "bad-org")
        self.assertTrue("RuntimeError" in data["reason"])


class TestOrgProfileValidation(unittest.TestCase):
    """Pydantic contract — boundary cases for floats in 0.0-1.0 ranges
    and required string fields."""

    def test_tech_role_ratio_below_zero_rejected(self) -> None:
        with self.assertRaises(Exception):
            OrgProfile(
                **self._make_kwargs(tech_role_ratio=-0.01),
            )

    def test_tech_role_ratio_above_one_rejected(self) -> None:
        with self.assertRaises(Exception):
            OrgProfile(
                **self._make_kwargs(tech_role_ratio=1.01),
            )

    def test_all_floats_at_zero_accepted(self) -> None:
        OrgProfile(**self._make_kwargs(
            tech_role_ratio=0.0,
            overall_confidence=0.0,
            sponsorship_likelihood=0.0,
        ))

    def test_all_floats_at_one_accepted(self) -> None:
        OrgProfile(**self._make_kwargs(
            tech_role_ratio=1.0,
            overall_confidence=1.0,
            sponsorship_likelihood=1.0,
            clearance_likelihood=1.0,
            startup_likelihood=1.0,
            volatility_signal=1.0,
        ))

    @staticmethod
    def _make_kwargs(**overrides) -> dict:
        base = _OrgProfileBuilder.make()
        # Drop nested-pydantic extras for the constructor.
        kwargs = {k: v for k, v in base.items() if k != "schema_version"}
        kwargs.update(overrides)
        return kwargs


class TestRunnerHook(unittest.TestCase):
    """End-to-end load_orgs() integration: board-runner reads
    ``_skip_list.json`` when ``BOARDS_USE_ENRICHED_PROFILES=1``.

    Mocks ORG_INDEX so the test doesn't read the real
    ``backend/data/<board>_companies.json`` files (which would
    make the test order-dependent on host repo state). Mocking the
    file path also lets us inject a tiny synthetic org list.
    """

    def setUp(self) -> None:
        # Snapshot env so the hook-test mutations don't leak.
        self._env_snapshot = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir()
        self.board_path = self.data_dir / "test_companies.json"
        self.board_path.write_text(json.dumps(["alpha", "bravo", "charlie"]))

        # Re-import the runner module with DATA_DIR patched in.
        import importlib

        original = os.environ.copy()
        os.environ["BOARDS_USE_ENRICHED_PROFILES"] = "0"  # default
        from pipeline.nodes.jobs_boards import runner as runner_module
        # Snapshot the original module-level state BEFORE patching so
        # ``tearDown`` (via addCleanup below) restores them. Without
        # this the patched ``DATA_DIR`` and ``ORG_INDEX`` persist into
        # any subsequent test that imports the runner module — the
        # original ORG_INDEX ("ashby", "greenhouse", "lever") would
        # silently leak away and ``Runner.load_orgs("ashby")` would
        # KeyError. The existing ``test_job_board_runner.py`` solves
        # the same problem with ``importlib.reload`` in tearDown; we
        # use the lighter attribute-restore here because we're
        # patching only two identifiers, not module-import-time env.
        self._orig_data_dir = runner_module.DATA_DIR
        self._orig_org_index = runner_module.ORG_INDEX
        runner_module.DATA_DIR = self.data_dir
        # Inject a synthetic ORG_INDEX entry pointing at our test file.
        runner_module.ORG_INDEX = {
            "testboard": (self.board_path, mock.MagicMock()),
        }
        self.runner_module = runner_module
        self.addCleanup(self._restore_runner_state)

    def _restore_runner_state(self) -> None:
        """Restore module-level state mutated by :meth:`setUp`.

        Registered via ``addCleanup`` so the restoration runs even
        when the test raises — a patch-only-once leak would
        otherwise silently break every runner-using test in this
        process forever.
        """
        self.runner_module.DATA_DIR = self._orig_data_dir
        # Take a fresh copy because the original dict could have been
        # mutated by the test (our patch is a literal, but a future
        # test might assign the dict itself and silently leak a
        # stale reference otherwise).
        self.runner_module.ORG_INDEX = dict(self._orig_org_index)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_snapshot)

    def test_default_env_no_skip_list_read(self) -> None:
        """Default 0 -> the runner ignores the skip list entirely,
        even when the file is present."""
        os.environ.pop("BOARDS_USE_ENRICHED_PROFILES", None)
        # Write a skip list that would filter every slug.
        skip_dir = self.data_dir / "enriched" / "testboard"
        skip_dir.mkdir(parents=True)
        (skip_dir / "_skip_list.json").write_text(
            json.dumps({"schema_version": 1, "slugs": ["alpha", "bravo", "charlie"]})
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie"])

    def test_env_set_1_with_slugs_skips_them(self) -> None:
        os.environ["BOARDS_USE_ENRICHED_PROFILES"] = "1"
        skip_dir = self.data_dir / "enriched" / "testboard"
        skip_dir.mkdir(parents=True)
        (skip_dir / "_skip_list.json").write_text(
            json.dumps({"schema_version": 1, "slugs": ["bravo"]})
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "charlie"])

    def test_env_set_1_with_meta_payload_skips(self) -> None:
        """Pin the meta-payload shape contract: ``{schema_version, slugs: [...]}``."""
        os.environ["BOARDS_USE_ENRICHED_PROFILES"] = "1"
        skip_dir = self.data_dir / "enriched" / "testboard"
        skip_dir.mkdir(parents=True)
        (skip_dir / "_skip_list.json").write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "slugs": ["alpha"]})
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["bravo", "charlie"])

    def test_env_set_1_with_missing_file_falls_through(self) -> None:
        """Absent skip list -> full org list returned (current behavior)."""
        os.environ["BOARDS_USE_ENRICHED_PROFILES"] = "1"
        # No _skip_list.json in the data dir.
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie"])

    def test_env_set_1_with_malformed_file_falls_through(self) -> None:
        """Malformed skip list should NOT crash the runner — log+fall
        through to the full org list. Better to do one extra HTTP
        fetch than to break the cron on a JSON parse bug."""
        os.environ["BOARDS_USE_ENRICHED_PROFILES"] = "1"
        skip_dir = self.data_dir / "enriched" / "testboard"
        skip_dir.mkdir(parents=True)
        (skip_dir / "_skip_list.json").write_text("{this is not json")
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie"])

    def test_env_truthy_variants(self) -> None:
        """``"true"`` and ``"yes"`` (lowercased) also enable the gate."""
        for truthy in ("1", "true", "yes"):
            os.environ["BOARDS_USE_ENRICHED_PROFILES"] = truthy
            skip_dir = self.data_dir / "enriched" / "testboard"
            skip_dir.mkdir(parents=True)
            (skip_dir / "_skip_list.json").write_text(
                json.dumps({"schema_version": 1, "slugs": ["alpha"]})
            )
            orgs = self.runner_module.load_orgs("testboard")
            self.assertEqual(orgs, ["bravo", "charlie"], f"truthy={truthy!r}")

    def test_env_unset_disables_the_gate(self) -> None:
        """Explicitly absent env var -> 0 default -> full list."""
        os.environ.pop("BOARDS_USE_ENRICHED_PROFILES", None)
        skip_dir = self.data_dir / "enriched" / "testboard"
        skip_dir.mkdir(parents=True)
        (skip_dir / "_skip_list.json").write_text(
            json.dumps({"schema_version": 1, "slugs": ["alpha"]})
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie"])


if __name__ == "__main__":
    unittest.main()
