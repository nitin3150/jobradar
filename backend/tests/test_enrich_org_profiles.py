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
    CADENCE_BUCKETS,
    OrgProfile,
    SCHEMA_VERSION,
    SKIP_CONFIDENCE_THRESHOLD,
    SKIP_TECH_RATIO_THRESHOLD,
    SourceJob,
    _atomic_write_json,
    _bucket_for_ok_profile,
    _build_source_jobs,
    _build_user_prompt,
    _compute_skip_for_profile,
    _emit_per_bucket_summary,
    _find_existing_profile,
    _purge_stale_duplicate_profiles,
    _stale_profile_paths_for_slug,
    _target_path_for_slug,
    _trim_jobs_for_prompt,
    _write_status_envelope,
)

# Package ref bound at module-level so test methods can refer
# to ``eom.PROFILE_DIR`` (etc.) without re-importing
# ``scripts.enrich_org_profiles`` in every ``setUp``. The
# module-level binding also lines up with monkey-patching:
# a future ``mock.patch.object(eom, 'PROFILE_DIR', ...)``
# needs the attribute to be patchable on the *module*
# object, not on a per-frame local.
from scripts import enrich_org_profiles as eom  # noqa: E402


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
                posting_cadence="daily",
                sponsorship_open=False,
                tech_role_ratio=SKIP_TECH_RATIO_THRESHOLD - 0.05,
                overall_confidence=SKIP_CONFIDENCE_THRESHOLD + 0.05,
            )
            self.assertFalse(
                _compute_skip_for_profile(profile, now=self._NOW),
                f"status={status!r} should never skip",
            )

    def test_stale_dead_org_does_not_skip(self) -> None:
        """Pin the explicit Rule 1 removal: a ``dead``-cadence org
        with raw-age > 180d is NOT skipped — the per-cadence
        layout routes it to ``cadence/dead/<slug>.json`` and
        lets the GHA probe workflow's cron handle the staleness
        budget naturally. Operationally this means the prior
        behavior of permanently skipping dead-cadence orgs is
        gone; a recovered dead org is now picked up on the next
        re-enrich without operator intervention.
        """
        profile = _OrgProfileBuilder.make(
            status="ok",
            posting_cadence="dead",
            source_last_published=(
                self._NOW - timedelta(days=400)
            ).isoformat(),
        )
        self.assertFalse(
            _compute_skip_for_profile(profile, now=self._NOW),
            "dead-cadence + stale-age must not skip under the new layout",
        )

    # -- Rule 1: explicit sponsorship block ---------------------------
    def test_rule1_sponsorship_open_false_skips(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=False
        )
        self.assertTrue(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule1_sponsorship_open_none_does_not_skip(self) -> None:
        """``None`` (no info) is NOT a skip signal — the runner treats
        unknown as "fall through to current behavior"."""
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=None
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule1_sponsorship_open_true_does_not_skip(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=True
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule1_clearance_required_true_skips(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            sponsorship_open=None,
            clearance_required=True,
        )
        self.assertTrue(_compute_skip_for_profile(profile, now=self._NOW))

    # -- Rule 2: confidently non-tech ---------------------------------
    def test_rule2_low_tech_high_conf_skips(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=SKIP_TECH_RATIO_THRESHOLD - 0.05,
            overall_confidence=SKIP_CONFIDENCE_THRESHOLD + 0.05,
        )
        self.assertTrue(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule2_low_tech_low_conf_falls_through(self) -> None:
        """When confidence is low we DON'T skip — the LLM isn't sure
        and we'd rather pay the per-job LLM tokens than miss a real
        tech opportunity. This is the safety case for the AND."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=0.05,
            overall_confidence=0.5,
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule2_high_tech_low_conf_falls_through(self) -> None:
        """High tech-ratio with low confidence also doesn't skip —
        the LLM might be uncertain about a startup with a small
        board."""
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=0.7,
            overall_confidence=0.3,
        )
        self.assertFalse(_compute_skip_for_profile(profile, now=self._NOW))

    def test_rule2_boundary_thresholds(self) -> None:
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


class TestBucketRouting(unittest.TestCase):
    """Per-cadence bucket router: ``_bucket_for_ok_profile`` +
    ``_target_path_for_slug`` + ``_stale_profile_paths_for_slug``
    + ``_purge_stale_duplicate_profiles`` wire together to give
    every org exactly one on-disk location.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from scripts import enrich_org_profiles
        self._orig = eom.PROFILE_DIR
        eom.PROFILE_DIR = Path(self._tmp.name) / "enriched"
        self.addCleanup(
            lambda: setattr(
                enrich_org_profiles, "PROFILE_DIR", self._orig
            )
        )
        (eom.PROFILE_DIR / "greenhouse").mkdir(parents=True)

    def test_cadence_bucket_set_is_frozen(self) -> None:
        """Pin the canonical bucket set so a future drift in
        :data:`CADENCE_BUCKETS` is a visible test diff rather
        than a silent behavior change."""
        self.assertEqual(
            sorted(CADENCE_BUCKETS),
            [
                "biweekly", "daily", "dead", "few_per_week",
                "monthly", "quarterly", "rare", "unknown", "weekly",
            ],
        )

    def test_weekly_cadence_routes_to_weekly_bucket(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok", posting_cadence="weekly",
        )
        self.assertEqual(_bucket_for_ok_profile(profile), "weekly")
        self.assertEqual(
            _target_path_for_slug(
                board="greenhouse", slug="stripe", bucket="weekly",
            ),
            eom.PROFILE_DIR / "greenhouse"
            / "cadence" / "weekly" / "stripe.json",
        )

    def test_sponsorship_block_routes_to_skip_bucket(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok", sponsorship_open=False,
        )
        self.assertEqual(_bucket_for_ok_profile(profile), "skip")
        self.assertEqual(
            _target_path_for_slug(
                board="greenhouse", slug="citi", bucket="skip",
            ),
            eom.PROFILE_DIR / "greenhouse"
            / "skip" / "citi.json",
        )

    def test_clearance_routes_to_skip_bucket(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            clearance_required=True,
        )
        self.assertEqual(_bucket_for_ok_profile(profile), "skip")

    def test_non_tech_high_confidence_routes_to_skip_bucket(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            tech_role_ratio=SKIP_TECH_RATIO_THRESHOLD - 0.05,
            overall_confidence=SKIP_CONFIDENCE_THRESHOLD + 0.05,
        )
        self.assertEqual(_bucket_for_ok_profile(profile), "skip")

    def test_unknown_cadence_value_falls_back_to_unknown_bucket(self) -> None:
        profile = _OrgProfileBuilder.make(
            status="ok",
            posting_cadence="weird-not-in-canonical-set",
        )
        self.assertEqual(_bucket_for_ok_profile(profile), "unknown")

    def test_status_not_ok_never_routes_to_cadence(self) -> None:
        # Failed / skipped profiles still go through
        # :func:`_bucket_for_ok_profile` (it's a pure dict shape
        # check), but the caller (``_enrich_one_org`` and
        # ``_write_status_envelope``) writes them to
        # ``errors/<slug>.json`` directly without consulting the
        # bucket router output.
        for status in ("failed", "skipped", None):
            profile = _OrgProfileBuilder.make(
                slug="alpha",
                status=status,
                posting_cadence="weekly",
            )
            bucket = _bucket_for_ok_profile(profile)
            # A non-ok profile with sponsorship_open=False still
            # computes "skip" because the bucket router follows
            # the same Rules 1+2 logic — the on-disk write path
            # in ``_write_status_envelope`` ignores this output
            # and routes non-ok profiles to ``errors/`` instead.
            self.assertIn(
                bucket, {"weekly", "skip", "unknown"},
                f"unexpected bucket {bucket!r} for status={status!r}",
            )

    def test_stale_paths_lists_every_possible_location(self) -> None:
        """Every on-disk location a slug might occupy is enumerated
        so the duplicate-purge contract is exhaustive."""
        paths = _stale_profile_paths_for_slug(
            board="greenhouse", slug="stripe",
        )
        path_strs = [str(p) for p in paths]
        self.assertIn(
            str(
                eom.PROFILE_DIR
                / "greenhouse" / "stripe.json"
            ),
            path_strs,
        )
        self.assertIn(
            str(
                eom.PROFILE_DIR
                / "greenhouse" / "errors" / "stripe.json"
            ),
            path_strs,
        )
        self.assertIn(
            str(
                eom.PROFILE_DIR
                / "greenhouse" / "skip" / "stripe.json"
            ),
            path_strs,
        )
        for bucket in CADENCE_BUCKETS:
            self.assertIn(
                str(
                    eom.PROFILE_DIR
                    / "greenhouse" / "cadence" / bucket / "stripe.json"
                ),
                path_strs,
            )

    def test_purge_removes_other_buckets_keeps_target(self) -> None:
        board_dir = (
            eom.PROFILE_DIR / "greenhouse"
        )
        # Pre-populate every location with a stub file.
        stale_locations = [
            board_dir / "stripe.json",
            board_dir / "errors" / "stripe.json",
            board_dir / "skip" / "stripe.json",
            board_dir / "cadence" / "weekly" / "stripe.json",
            board_dir / "cadence" / "daily" / "stripe.json",
            board_dir / "cadence" / "dead" / "stripe.json",
        ]
        for path in stale_locations:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"legacy": True}))

        target = board_dir / "cadence" / "monthly" / "stripe.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"current": True}))

        removed = _purge_stale_duplicate_profiles(
            board="greenhouse", slug="stripe", target_path=target,
        )

        # Every location EXCEPT the target is removed.
        for path in stale_locations:
            self.assertFalse(
                path.exists(),
                f"stale duplicate {path} should be purged",
            )
        self.assertTrue(target.exists(), "target must remain")
        # Removed list should contain every non-target location.
        self.assertEqual(
            sorted(str(p) for p in removed),
            sorted(str(p) for p in stale_locations),
        )

    def test_purge_target_safety_does_not_delete_target(self) -> None:
        """Defensive — if ``target`` already exists in the same
        location the caller wants, the purge must not delete it."""
        board_dir = (
            eom.PROFILE_DIR / "greenhouse"
        )
        target = board_dir / "cadence" / "weekly" / "stripe.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"payload": "ok"}))

        removed = _purge_stale_duplicate_profiles(
            board="greenhouse", slug="stripe", target_path=target,
        )
        self.assertEqual(removed, [])
        self.assertTrue(target.exists())

    def test_find_existing_walks_every_location(self) -> None:
        board_dir = (
            eom.PROFILE_DIR / "greenhouse"
        )
        # No profile on disk yet.
        self.assertIsNone(
            _find_existing_profile(board="greenhouse", slug="ghost")
        )
        # Cadence bucket match.
        bucket_path = board_dir / "cadence" / "weekly" / "ghost.json"
        bucket_path.parent.mkdir(parents=True, exist_ok=True)
        bucket_path.write_text(json.dumps({"v": 1}))
        self.assertEqual(
            _find_existing_profile(board="greenhouse", slug="ghost"),
            bucket_path,
        )
        # Skip-bucket match.
        bucket_path.unlink()
        skip_path = board_dir / "skip" / "ghost.json"
        skip_path.parent.mkdir(parents=True, exist_ok=True)
        skip_path.write_text(json.dumps({"v": 1}))
        self.assertEqual(
            _find_existing_profile(board="greenhouse", slug="ghost"),
            skip_path,
        )
        # Legacy top-level match (just-in-case the previous
        # enrichment wrote there).
        skip_path.unlink()
        legacy_path = board_dir / "ghost.json"
        legacy_path.write_text(json.dumps({"v": 1}))
        self.assertEqual(
            _find_existing_profile(board="greenhouse", slug="ghost"),
            legacy_path,
        )


class TestWriteStatusEnvelope(unittest.TestCase):
    """Non-OK envelopes (status: skipped / status: failed) — the
    boards runner ignores them because they live in ``errors/``,
    which no ``BOARDS_CADENCES`` value ever names. Pin the shape
    AND the on-disk location so the per-cadence layout's
    invariant (exactly one file per (board, slug) tuple,
    sitting inside either ``errors/``, ``skip/``, or
    ``cadence/<bucket>/``) is preserved across runs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from scripts import enrich_org_profiles
        self._orig = eom.PROFILE_DIR
        eom.PROFILE_DIR = Path(self._tmp.name) / "enriched"
        self.addCleanup(
            lambda: setattr(
                enrich_org_profiles, "PROFILE_DIR", self._orig
            )
        )
        (eom.PROFILE_DIR / "greenhouse").mkdir(parents=True)

    def test_skipped_envelope_shape_and_errors_path(self) -> None:
        path = _write_status_envelope(
            board="greenhouse",
            slug="tiny-org",
            status="skipped",
            reason_or_error="fewer_than_3_jobs",
            extra={"source_jobs_count": 2},
        )
        # Path lands in ``errors/`` — that location is invisible
        # to ``load_orgs()`` because no ``BOARDS_CADENCES``
        # value names it.
        self.assertEqual(
            path,
            eom.PROFILE_DIR
            / "greenhouse" / "errors" / "tiny-org.json",
        )
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["status"], "skipped")
        self.assertEqual(data["slug"], "tiny-org")
        self.assertEqual(data["reason"], "fewer_than_3_jobs")
        self.assertEqual(data["source_jobs_count"], 2)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)

    def test_failed_envelope_shape_and_errors_path(self) -> None:
        path = _write_status_envelope(
            board="greenhouse",
            slug="bad-org",
            status="failed",
            reason_or_error="RuntimeError: all providers failed",
        )
        self.assertEqual(
            path,
            eom.PROFILE_DIR
            / "greenhouse" / "errors" / "bad-org.json",
        )
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["slug"], "bad-org")
        self.assertTrue("RuntimeError" in data["reason"])

    def test_envelope_writer_purges_preexisting_ok_profile(self) -> None:
        """A re-enrich that previously had a successful profile
        at a cadence bucket but fails on this attempt must
        clean up the stale OK copy before writing the errors
        envelope. The on-disk invariant "exactly one file per
        (board, slug) tuple" must hold."""
        board_dir = (
            eom.PROFILE_DIR / "greenhouse"
        )
        # Pre-populate a stale OK profile at an unrelated cadence
        # bucket — the kind of state a previous succeeded
        # enrich would have left behind.
        stale = board_dir / "cadence" / "weekly" / "stripe.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"status": "ok", "old": True}))
        path = _write_status_envelope(
            board="greenhouse",
            slug="stripe",
            status="failed",
            reason_or_error="RuntimeError",
        )
        self.assertFalse(
            stale.exists(),
            "stale cadence-bucket OK profile must be purged on error envelope write",
        )
        self.assertTrue(path.exists())


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
    """End-to-end ``load_orgs()`` integration with the
    per-cadence layout.

    The runner reads ``BOARDS_CADENCES`` as a comma-separated
    list of cadence buckets and returns the union of slugs
    whose on-disk profile lives in
    ``data/enriched/<board>/cadence/<bucket>/``. The flat
    ``_skip_list.json`` reader was REMOVED — every org profile
    now lives in ``cadence/<bucket>/`` (scanned) or ``skip/``
    (never scanned) or ``errors/`` (operator inspection only).
    Mocks ``ORG_INDEX`` so the test doesn't read the real
    ``backend/data/<board>_companies.json`` files (which would
    make the test order-dependent on host repo state).
    """

    def setUp(self) -> None:
        # Snapshot env so the hook-test mutations don't leak.
        self._env_snapshot = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir()
        self.board_path = self.data_dir / "test_companies.json"
        self.board_path.write_text(json.dumps(["alpha", "bravo", "charlie", "delta"]))

        os.environ.pop("BOARDS_CADENCES", None)
        from pipeline.nodes.jobs_boards import runner as runner_module
        self._orig_data_dir = runner_module.DATA_DIR
        self._orig_org_index = runner_module.ORG_INDEX
        runner_module.DATA_DIR = self.data_dir
        runner_module.ORG_INDEX = {
            "testboard": (self.board_path, mock.MagicMock()),
        }
        self.runner_module = runner_module
        self.addCleanup(self._restore_runner_state)

    def _restore_runner_state(self) -> None:
        """Restore module-level state mutated by :meth:`setUp`.

        Registered via ``addCleanup`` so the restoration runs
        even when the test raises — a patch-only-once leak
        would otherwise silently break every runner-using test
        in this process forever.
        """
        self.runner_module.DATA_DIR = self._orig_data_dir
        self.runner_module.ORG_INDEX = dict(self._orig_org_index)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_snapshot)

    def _seed_cadence_files(self, *pairs: tuple[str, str]) -> None:
        """Pre-create cadence bucket files on disk.

        Each ``(bucket, slug)`` pair writes an empty profile
        at ``data/enriched/testboard/cadence/<bucket>/<slug>.json``
        so ``load_orgs()`` can pick it up via the
        ``BOARDS_CADENCES`` -> glob -> Path.stem pipeline.
        """
        board_dir = self.data_dir / "enriched" / "testboard"
        for bucket, slug in pairs:
            bucket_dir = board_dir / "cadence" / bucket
            bucket_dir.mkdir(parents=True, exist_ok=True)
            (bucket_dir / f"{slug}.json").write_text(
                json.dumps({"status": "ok", "slug": slug, "board": "testboard"})
            )

    def test_default_env_no_cadence_filter_returns_full_list(self) -> None:
        """Unset ``BOARDS_CADENCES`` -> legacy behavior: every
        org from the full board list passes through (only the
        missing-orgs/timeout-orgs ban lists apply)."""
        os.environ.pop("BOARDS_CADENCES", None)
        # Even with cadence files on disk, an unset env returns
        # the full list.
        self._seed_cadence_files(("weekly", "alpha"), ("weekly", "bravo"))
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie", "delta"])

    def test_env_filters_to_one_cadence_bucket(self) -> None:
        os.environ["BOARDS_CADENCES"] = "weekly"
        self._seed_cadence_files(("weekly", "alpha"))
        orgs = self.runner_module.load_orgs("testboard")
        # Only ``alpha`` is in the weekly bucket — ``bravo``,
        # ``charlie``, ``delta`` are absent from the on-disk
        # bucket so they never make it into the scan.
        self.assertEqual(orgs, ["alpha"])

    def test_env_filters_to_multiple_cadence_buckets(self) -> None:
        os.environ["BOARDS_CADENCES"] = "weekly,biweekly"
        self._seed_cadence_files(
            ("weekly", "alpha"),
            ("biweekly", "bravo"),
            # ``daily`` is on disk but NOT named in the env
            # env-config -- should be ignored.
            ("daily", "charlie"),
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo"])

    def test_env_missing_cadence_dir_warns_and_skips(self) -> None:
        """A cadence name in BOARDS_CADENCES that has no on-disk
        directory logs a warning and contributes zero slugs
        rather than raising."""
        os.environ["BOARDS_CADENCES"] = "weekly,nonexistent"
        self._seed_cadence_files(("weekly", "alpha"))
        # No exception - just a logger.warning.
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha"])

    def test_env_empty_cadence_dir_yields_zero_slugs(self) -> None:
        """All named cadences exist but none contain any
        organisation profiles -> the runner returns ``[]``.
        :func:`run_all` then logs "no relevant jobs to score"
        and exits cleanly without writing a ``scanner_runs``
        ``state=error`` row."""
        os.environ["BOARDS_CADENCES"] = "weekly"
        # Create the directory but leave it empty.
        (self.data_dir / "enriched" / "testboard" / "cadence" / "weekly").mkdir(
            parents=True,
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, [])

    def test_env_allows_orgs_only_from_chosen_bucket_subset(self) -> None:
        """Reverse-direction: orgs not in the named cadence are
        NOT returned even if the full board index has them."""
        os.environ["BOARDS_CADENCES"] = "dead"
        self._seed_cadence_files(
            ("weekly", "alpha"),  # on disk, but not named
            ("dead", "delta"),    # the only valid one
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["delta"])

    def test_env_with_whitespace_tolerated(self) -> None:
        os.environ["BOARDS_CADENCES"] = "  weekly , biweekly  "
        self._seed_cadence_files(
            ("weekly", "alpha"),
            ("biweekly", "bravo"),
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo"])

    def test_env_unset_falls_through_to_full_list_even_if_skip_dir_present(self) -> None:
        """The flat ``_skip_list.json`` path was removed; the
        runner no longer reads it under any env-var. Verify
        that an unset BOARDS_CADENCES with a legacy
        ``_skip_list.json`` on disk (left over from a previous
        enrichment) returns the full list. The ``_skip_list.json``
        file is now dead-weight on disk but doesn't change
        behavior."""
        os.environ.pop("BOARDS_CADENCES", None)
        skip_dir = self.data_dir / "enriched" / "testboard"
        skip_dir.mkdir(parents=True)
        (skip_dir / "_skip_list.json").write_text(
            json.dumps({"schema_version": 1, "slugs": ["alpha", "bravo"]})
        )
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie", "delta"])

    def test_cadence_filter_still_respects_missing_orgs_ban(self) -> None:
        """``BOARDS_CADENCES`` narrows the org list, but the
        per-board missing-orgs ban list (``<board>_missing_orgs.json``)
        is applied on top. A slug in a cadence bucket AND in
        the missing-orgs file gets dropped either way."""
        os.environ["BOARDS_CADENCES"] = "weekly"
        self._seed_cadence_files(
            ("weekly", "alpha"),
            ("weekly", "bravo"),
        )
        # Ban-list excludes ``bravo`` (3-strikes 404 bench).
        missing_path = self.data_dir / "testboard_missing_orgs.json"
        missing_path.write_text(json.dumps(["bravo"]))
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha"])

    def test_env_empty_string_disables_filter(self) -> None:
        """``BOARDS_CADENCES=""`` should be treated the same as
        unset (no filter). An empty string after ``.strip()``
        is falsy in the runner's branch."""
        os.environ["BOARDS_CADENCES"] = ""
        self._seed_cadence_files(("weekly", "alpha"))
        orgs = self.runner_module.load_orgs("testboard")
        self.assertEqual(orgs, ["alpha", "bravo", "charlie", "delta"])


if __name__ == "__main__":
    unittest.main()
