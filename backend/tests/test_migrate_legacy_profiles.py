"""Tests for ``backend/scripts/migrate_legacy_profiles.py``.

Uses a per-test temp dir patched onto ``PROFILE_DIR`` so the migrator
reads/writes a private directory tree. The migrator does NOT need the
``LLMClient`` chain and never fires an LLM call — these tests are
hermetic + fast.

Each test stubs a small on-disk tree:

    <tmp>/enriched/<board>/
        <slug>.json              <-- legacy top-level file
        _skip_list.json          <-- legacy skip-meta (optional)
        skip/<slug>.json         <-- partial-state drift (optional)
        cadence/<bucket>/<slug>.json  <-- partial-state drift (optional)
        errors/<slug>.json       <-- partial-state drift (optional)

The test then asserts the post-migration tree matches expectations,
including unlinks of stale duplicates.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts import migrate_legacy_profiles as mlm
from scripts import enrich_org_profiles as eom


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
def _make_ok_profile(
    *,
    slug: str,
    board: str,
    posting_cadence: str = "weekly",
    sponsorship_open: bool | None = None,
    clearance_required: bool | None = None,
    tech_role_ratio: float = 0.5,
    overall_confidence: float = 0.8,
    schema_version: int = 2,
    source_jobs_count: int = 10,
    source_last_published: str = "2026-07-08T18:00:00+00:00",
    extra: dict | None = None,
) -> dict[str, Any]:
    """Build a minimal OrgProfile-shaped payload at the given slug."""
    payload = {
        "schema_version": schema_version,
        "slug": slug,
        "board": board,
        "enriched_at": "2026-07-14T05:00:00+00:00",
        "source_jobs_count": source_jobs_count,
        "source_last_published": source_last_published,
        "source_jobs": [],
        "primary_function": "engineering_heavy",
        "estimated_stage": "series_b",
        "hiring_volume_estimate": "10_50",
        "posting_cadence": posting_cadence,
        "sponsorship_open": sponsorship_open,
        "clearance_required": clearance_required,
        "remote_friendly": True,
        "is_likely_startup": False,
        "tech_role_ratio": tech_role_ratio,
        "sponsorship_likelihood": 0.5,
        "clearance_likelihood": 0.0,
        "startup_likelihood": 0.3,
        "volatility_signal": 0.1,
        "notes": "fixture",
        "overall_confidence": overall_confidence,
        "model_used": "meta/llama-3.1-70b-instruct",
        "status": "ok",
    }
    if extra:
        payload.update(extra)
    return payload


def _make_failed_envelope(
    *,
    slug: str,
    board: str,
    schema_version: int = 2,
    status: str = "failed",
    reason: str = "fixture",
    source_jobs_count: int | None = None,
) -> dict[str, Any]:
    """Build a status=failed/skipped envelope."""
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "status": status,
        "slug": slug,
        "board": board,
        "decision_at": "2026-07-14T05:00:00+00:00",
        "reason": reason,
    }
    if source_jobs_count is not None:
        payload["source_jobs_count"] = source_jobs_count
    return payload


def _read(p: Path) -> dict:
    """Read + json.load a path. Module-level helper used by tests."""
    with open(p, "r") as f:
        return json.load(f)


class _MigrationTestBase(unittest.TestCase):
    """Sets up a tmp enriched/ dir + patches ``PROFILE_DIR``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Save BOTH module-level bindings — the migrator imports PROFILE_DIR
        # via ``from scripts.enrich_org_profiles import PROFILE_DIR`` so the
        # binding at import-time is the original value, not a live reference.
        # Patching only ``eom.PROFILE_DIR`` would leave ``mlm.PROFILE_DIR``
        # pointing at the real on-disk path and every relative_to() / glob()
        # call inside the migrator would still target production data.
        self._orig_eom_PROFILE_DIR = eom.PROFILE_DIR
        self._orig_mlm_PROFILE_DIR = mlm.PROFILE_DIR
        self._tmp_root = Path(self._tmp.name)
        new_profile_dir = self._tmp_root / "enriched"
        new_profile_dir.mkdir(parents=True, exist_ok=True)
        eom.PROFILE_DIR = new_profile_dir
        mlm.PROFILE_DIR = new_profile_dir

    def tearDown(self) -> None:
        eom.PROFILE_DIR = self._orig_eom_PROFILE_DIR
        mlm.PROFILE_DIR = self._orig_mlm_PROFILE_DIR

    def _board(self, name: str) -> Path:
        d = eom.PROFILE_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_legacy(self, board_name: str, slug: str, payload: dict) -> Path:
        path = eom.PROFILE_DIR / board_name / f"{slug}.json"
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def _write_partial_state_file(
        self, board_name: str, sub: str, slug: str, payload: dict,
    ) -> Path:
        dir_ = eom.PROFILE_DIR / board_name / sub
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{slug}.json"
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def _read(self, p: Path) -> dict:
        with open(p, "r") as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Classification helpers — pure-function unit tests
# ---------------------------------------------------------------------------
class TestClassifyMigrationTarget(_MigrationTestBase):
    def test_status_ok_with_clean_cadence_routes_to_cadence_bucket(self) -> None:
        """An OK profile with weekly cadence + no skip rule hits
        → ``cadence/weekly/<slug>.json``."""
        payload = _make_ok_profile(
            slug="stripe", board="greenhouse", posting_cadence="weekly",
        )
        target, normalized, label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="stripe",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "stripe.json",
        )
        self.assertEqual(normalized["schema_version"], mlm.SCHEMA_VERSION)
        self.assertIn("ok → weekly", label)

    def test_status_ok_with_sponsor_closed_routes_to_skip(self) -> None:
        """Rule 1a: sponsorship_open=False directs to skip/."""
        payload = _make_ok_profile(
            slug="aclu", board="greenhouse",
            posting_cadence="weekly",
            sponsorship_open=False,
        )
        target, _normalized, label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="aclu",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "skip" / "aclu.json",
        )
        self.assertIn("ok → skip", label)

    def test_status_ok_with_clearance_required_routes_to_skip(self) -> None:
        """Rule 1b: clearance_required=True directs to skip/."""
        payload = _make_ok_profile(
            slug="ts-sci-org", board="greenhouse",
            posting_cadence="weekly",
            clearance_required=True,
        )
        target, _normalized, _label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="ts-sci-org",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "skip" / "ts-sci-org.json",
        )

    def test_status_ok_with_low_tech_high_confidence_routes_to_skip(self) -> None:
        """Rule 2: tech_role_ratio < 0.15 AND confidence > 0.7 → skip/."""
        payload = _make_ok_profile(
            slug="non-tech-org", board="greenhouse",
            posting_cadence="weekly",
            tech_role_ratio=0.10,
            overall_confidence=0.85,
        )
        target, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="non-tech-org",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "skip" / "non-tech-org.json",
        )

    def test_status_ok_with_low_tech_low_confidence_stays_cadence(self) -> None:
        """Rule 2 is conjunction — borderline LLM call should NOT skip."""
        payload = _make_ok_profile(
            slug="borderline-org", board="greenhouse",
            posting_cadence="weekly",
            tech_role_ratio=0.10,    # low tech
            overall_confidence=0.65, # borderline (NOT > 0.7)
        )
        target, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="borderline-org",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "borderline-org.json",
        )

    def test_status_ok_with_unknown_cadence_coerces_to_unknown_bucket(self) -> None:
        """A posting_cadence value not in CADENCE_BUCKETS (e.g. LLM drift)
        → ``cadence/unknown/<slug>.json``."""
        payload = _make_ok_profile(
            slug="drift-org", board="greenhouse",
            posting_cadence="rythmic",  # not in CADENCE_BUCKETS
        )
        target, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="drift-org",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "cadence" / "unknown" / "drift-org.json",
        )

    def test_status_failed_envelope_routes_to_errors(self) -> None:
        payload = _make_failed_envelope(slug="1inch", board="lever")
        target, normalized, label = mlm._classify_migration_target(
            payload, board="lever", slug="1inch",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "lever" / "errors" / "1inch.json",
        )
        self.assertEqual(normalized["status"], "failed")
        self.assertIn("failed", label)

    def test_status_skipped_envelope_routes_to_errors(self) -> None:
        payload = _make_failed_envelope(slug="fewer-than-3", board="lever", status="skipped")
        target, normalized, _label = mlm._classify_migration_target(
            payload, board="lever", slug="fewer-than-3",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "lever" / "errors" / "fewer-than-3.json",
        )
        self.assertEqual(normalized["status"], "skipped")

    def test_unknown_status_routes_to_errors(self) -> None:
        """A malformed legacy file with status=weird goes to errors for
        operator inspection; boards runner never reads errors/."""
        payload = {"schema_version": 1, "status": "weird", "slug": "x",
                   "board": "greenhouse"}
        target, _n, label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="x",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "errors" / "x.json",
        )
        self.assertIn("unknown_status", label)

    def test_missing_status_routes_to_errors(self) -> None:
        payload = {"schema_version": 1, "slug": "x", "board": "greenhouse"}
        target, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="x",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "errors" / "x.json",
        )

    def test_v1_schema_is_bumped_to_SCHEMA_VERSION(self) -> None:
        """Schema-version migration: a legacy v1 file gets stamped v2."""
        payload = _make_ok_profile(
            slug="v1-org", board="greenhouse",
            posting_cadence="weekly", schema_version=1,
        )
        _target, normalized, _label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="v1-org",
        )
        self.assertEqual(normalized["schema_version"], mlm.SCHEMA_VERSION)

    def test_input_dict_is_not_mutated(self) -> None:
        """The original payload argument should NOT have schema_version
        rewritten in place — operators reading the legacy file mid-run
        must see the pre-migration bytes still.

        Shape-OK + no-status routing (Case D) stamps ``status="ok"`` on
        the *copy* (not the original); this test pins that contract by
        using a status=ok fixture. The shape-OK no-status path is
        covered separately by ``test_shape_ok_no_status_input_not_mutated``
        below.
        """
        payload = _make_ok_profile(
            slug="mut-test", board="greenhouse",
            posting_cadence="weekly", schema_version=1,
        )
        snapshot = dict(payload)
        _t, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="mut-test",
        )
        # Input dict is byte-identical to the pre-migration snapshot —
        # the schema_version rewrite (and any other mutation) lands on
        # the copy, not the input.
        self.assertEqual(payload, snapshot)

    # ---------- New: bug-fix routing cases ----------
    def test_status_skipped_with_fewer_than_reason_routes_to_cadence_rare(self) -> None:
        """Case B: status=skipped + reason starts with ``fewer_than_`` +
        source_jobs_count > 0 → ``cadence/rare/<slug>.json``. These are
        small-but-real orgs the enrichment script skipped too early;
        they belong on the weekly probe (rare cadence) rather than in
        errors/ where the boards runner never reads them."""
        payload = _make_failed_envelope(
            slug="ardea-partners", board="ashby",
            status="skipped", reason="fewer_than_3_jobs",
            source_jobs_count=1,
        )
        target, normalized, label = mlm._classify_migration_target(
            payload, board="ashby", slug="ardea-partners",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "ashby" / "cadence" / "rare" / "ardea-partners.json",
        )
        self.assertEqual(normalized["schema_version"], mlm.SCHEMA_VERSION)
        self.assertIn("rare", label)
        self.assertEqual(normalized["status"], "skipped")  # preserved

    def test_status_skipped_with_fewer_than_but_zero_jobs_routes_to_errors(self) -> None:
        """Defensive: if reason matches but source_jobs_count is 0
        (e.g. partial-write / corrupted envelope), fall through to
        errors/ so the boards runner doesn't pick up a zero-data row."""
        payload = _make_failed_envelope(
            slug="weird-empty", board="ashby",
            status="skipped", reason="fewer_than_3_jobs",
            source_jobs_count=0,
        )
        target, _n, _l = mlm._classify_migration_target(
            payload, board="ashby", slug="weird-empty",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "ashby" / "errors" / "weird-empty.json",
        )

    def test_shape_ok_no_status_routes_to_cadence_bucket(self) -> None:
        """Case D: a v2 OrgProfile-shaped payload written WITHOUT the
        ``status: ok`` stamp (older enrichment-script versions) gets
        routed to its cadence bucket, not punted to errors/."""
        payload = _make_ok_profile(
            slug="vts-old-stamp", board="greenhouse",
            posting_cadence="weekly",
        )
        # Older version of the enrichment script wrote the profile
        # dump without stamping status — simulate by deleting the key.
        del payload["status"]
        target, normalized, label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="vts-old-stamp",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "vts-old-stamp.json",
        )
        # The normalized payload gets status="ok" stamped so the
        # on-disk file is consistent with current contract.
        self.assertEqual(normalized["status"], "ok")
        self.assertIn("weekly", label)

    def test_shape_ok_no_status_with_sponsor_block_routes_to_skip(self) -> None:
        """Case D edge: a shape-OK no-status payload with sponsor-block
        still gets caught by Rule 1 because we stamp status="ok"
        before delegating to ``_bucket_for_ok_profile``. Without the
        stamp, ``_compute_skip_for_profile`` short-circuits and the
        org lands in cadence/unknown/ instead of skip/."""
        payload = _make_ok_profile(
            slug="no-visa-no-stamp", board="greenhouse",
            posting_cadence="weekly", sponsorship_open=False,
        )
        del payload["status"]
        target, _n, label = mlm._classify_migration_target(
            payload, board="greenhouse", slug="no-visa-no-stamp",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "skip" / "no-visa-no-stamp.json",
        )
        self.assertIn("skip", label)

    def test_shape_ok_no_status_with_zero_jobs_routes_to_errors(self) -> None:
        """Case D fallback A: status is None, posting_cadence is set,
        source_jobs_count == 0 → can't trust shape-only heuristics on
        sparse data, route to errors/ for operator inspection."""
        payload = _make_ok_profile(
            slug="sparse-shape", board="greenhouse",
            posting_cadence="weekly", source_jobs_count=0,
        )
        del payload["status"]
        target, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="sparse-shape",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "errors" / "sparse-shape.json",
        )

    def test_shape_ok_no_status_with_missing_cadence_routes_to_errors(self) -> None:
        """Case D fallback B: status is None, source_jobs_count > 0,
        but posting_cadence field is also missing → can't bucket, route
        to errors/."""
        payload = _make_ok_profile(
            slug="missing-cadence", board="greenhouse",
            posting_cadence="weekly", source_jobs_count=5,
        )
        del payload["status"]
        del payload["posting_cadence"]
        target, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="missing-cadence",
        )
        self.assertEqual(
            target, eom.PROFILE_DIR / "greenhouse" / "errors" / "missing-cadence.json",
        )

    def test_shape_ok_no_status_input_not_mutated(self) -> None:
        """Case D stamps ``status="ok"`` on the copy, not the input —
        same mutation guarantee as the schema_version rewrite. The
        on-disk file gets the stamp; the mid-run read of the legacy
        file does not."""
        payload = _make_ok_profile(
            slug="no-mut-stamp-test", board="greenhouse",
            posting_cadence="weekly",
        )
        del payload["status"]
        snapshot = dict(payload)
        self.assertNotIn("status", snapshot)
        _t, _n, _l = mlm._classify_migration_target(
            payload, board="greenhouse", slug="no-mut-stamp-test",
        )
        self.assertEqual(payload, snapshot)
        self.assertNotIn("status", payload)


# ---------------------------------------------------------------------------
# Plan actions — write/skip/unlink sequencing
# ---------------------------------------------------------------------------
class TestPlanActionsForSlug(_MigrationTestBase):
    def test_clean_migration_writes_target_and_unlinks_legacy(self) -> None:
        """OK profile at top level, no drift anywhere → write to target
        + unlink the legacy top-level file."""
        self._board("greenhouse")
        self._write_legacy(
            "greenhouse", "stripe",
            _make_ok_profile(slug="stripe", board="greenhouse",
                             posting_cadence="weekly"),
        )
        payload = _read(eom.PROFILE_DIR / "greenhouse" / "stripe.json")
        actions = mlm._plan_actions_for_slug(
            board="greenhouse", slug="stripe", payload=payload,
        )
        kinds = [a[0] for a in actions]
        self.assertIn("write", kinds)
        self.assertIn("unlink_legacy", kinds)
        self.assertNotIn("unlink_drift", kinds)
        self.assertNotIn("skip_existing", kinds)

    def test_drift_unlinks_extra_cadence_copy(self) -> None:
        """Two copies of the same slug — top-level + cadence/daily/ —
        produce drift-detection on the wrong-bucket copy."""
        self._board("greenhouse")
        payload = _make_ok_profile(
            slug="acme", board="greenhouse", posting_cadence="weekly",
        )
        self._write_legacy("greenhouse", "acme", payload)
        # Wrong-bucket drift copy at cadence/daily/
        self._write_partial_state_file(
            "greenhouse", "cadence/daily", "acme", payload,
        )
        actions = mlm._plan_actions_for_slug(
            board="greenhouse", slug="acme", payload=payload,
        )
        kinds = [a[0] for a in actions]
        self.assertIn("write", kinds)
        self.assertIn("unlink_legacy", kinds)
        self.assertIn("unlink_drift", kinds)
        # Drift is on the cadence/daily copy — that's target_path is weekly
        drift_paths = [a[1] for a in actions if a[0] == "unlink_drift"]
        self.assertEqual(len(drift_paths), 1)
        self.assertTrue(str(drift_paths[0]).endswith("cadence/daily/acme.json"))

    def test_target_already_present_skips_write(self) -> None:
        """If the per-cadence file is already at its correct destination,
        we trust it and skip the write step (don't clobber); we still
        unlink the legacy top-level because it's a duplicate now."""
        self._board("greenhouse")
        payload = _make_ok_profile(
            slug="bravo", board="greenhouse", posting_cadence="weekly",
        )
        self._write_legacy("greenhouse", "bravo", payload)
        # Pretend an earlier run already landed bravo correctly.
        self._write_partial_state_file(
            "greenhouse", "cadence/weekly", "bravo", dict(payload),
        )
        actions = mlm._plan_actions_for_slug(
            board="greenhouse", slug="bravo", payload=payload,
        )
        kinds = [a[0] for a in actions]
        self.assertIn("skip_existing", kinds)
        self.assertNotIn("write", kinds)
        self.assertIn("unlink_legacy", kinds)


# ---------------------------------------------------------------------------
# End-to-end migrate_board
# ---------------------------------------------------------------------------
class TestMigrateBoard(_MigrationTestBase):
    def test_full_apply_with_mixed_payloads(self) -> None:
        """Mix of OK + sponsor-blocked + non-tech + failed envelopes;
        apply=True moves each to the correct destination and unlinks
        nothing it shouldn't."""
        board = self._board("greenhouse")
        # OK, weekly, clean
        self._write_legacy(
            "greenhouse", "alpha",
            _make_ok_profile(slug="alpha", board="greenhouse",
                             posting_cadence="weekly"),
        )
        # OK but sponsor-blocked → skip/
        self._write_legacy(
            "greenhouse", "no-visa",
            _make_ok_profile(slug="no-visa", board="greenhouse",
                             posting_cadence="weekly",
                             sponsorship_open=False),
        )
        # OK but confidently non-tech → skip/
        self._write_legacy(
            "greenhouse", "low-tech-org",
            _make_ok_profile(slug="low-tech-org", board="greenhouse",
                             posting_cadence="monthly",
                             tech_role_ratio=0.05,
                             overall_confidence=0.9),
        )
        # Failed envelope → errors/
        self._write_legacy(
            "greenhouse", "rate-limited",
            _make_failed_envelope(slug="rate-limited", board="greenhouse",
                                  status="failed"),
        )
        # Meta file
        with open(board / "_skip_list.json", "w") as f:
            json.dump({"schema_version": 1, "board": "greenhouse",
                       "computed_at": "x", "slugs": ["alpha"]}, f)

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=True,
        )

        self.assertEqual(summary["scanned"], 4)
        self.assertEqual(summary["migrated"], 4)
        self.assertEqual(summary["json_error"], 0)
        self.assertEqual(summary["meta_renamed"], 1)

        # Files at expected destinations.
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "alpha.json").exists(),
        )
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "skip" / "no-visa.json").exists(),
        )
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "skip" / "low-tech-org.json").exists(),
        )
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "errors" / "rate-limited.json").exists(),
        )

        # Legacy top-level files are gone.
        for slug in ("alpha", "no-visa", "low-tech-org", "rate-limited"):
            self.assertFalse((eom.PROFILE_DIR / "greenhouse" / f"{slug}.json").exists())

        # Meta file renamed, original gone.
        self.assertFalse((board / "_skip_list.json").exists())
        self.assertTrue((board / "_skip_list.deprecated.json").exists())

        # Schema-version normalization: the alpha file (was v2 in the
        # input) preserves v2 but the rate-limited envelope's schema
        # version was also v2 so still v2.
        alpha_data = _read(
            eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "alpha.json",
        )
        self.assertEqual(alpha_data["schema_version"], mlm.SCHEMA_VERSION)

    def test_dry_run_does_not_move_or_unlink(self) -> None:
        """apply=False → no writes, no unlinks, but printed plan."""
        board = self._board("greenhouse")
        self._write_legacy(
            "greenhouse", "alpha",
            _make_ok_profile(slug="alpha", board="greenhouse",
                             posting_cadence="weekly"),
        )
        with open(board / "_skip_list.json", "w") as f:
            json.dump({"schema_version": 1, "board": "greenhouse",
                       "computed_at": "x", "slugs": ["alpha"]}, f)

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=False,
            include_meta=True,
        )
        # Migrated count tracks "would-write" too — operations are
        # counted regardless of dry-run so the operator sees the plan.
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["migrated"], 1)

        # BUT the legacy file still exists (nothing actually moved).
        self.assertTrue((eom.PROFILE_DIR / "greenhouse" / "alpha.json").exists())
        self.assertFalse(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "alpha.json").exists(),
        )
        # Meta-file rename was a no-op too.
        self.assertTrue((board / "_skip_list.json").exists())
        self.assertFalse((board / "_skip_list.deprecated.json").exists())

    def test_idempotent_on_second_run(self) -> None:
        """First run migrates; second run finds no legacy top-level files
        (alpha was physically unlinked) so it scans 0 + safe no-op."""
        self._board("greenhouse")
        self._write_legacy(
            "greenhouse", "alpha",
            _make_ok_profile(slug="alpha", board="greenhouse",
                             posting_cadence="weekly"),
        )

        s1 = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=True,
        )
        # First run mutates the on-disk state — alpha.json no longer exists.
        self.assertFalse((eom.PROFILE_DIR / "greenhouse" / "alpha.json").exists())
        self.assertEqual(s1["migrated"], 1)
        self.assertEqual(s1["unlink_legacy"], 1)

        s2 = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=True,
        )
        # Second run scans zero legacy files and does zero migration.
        self.assertEqual(s2["scanned"], 0)
        self.assertEqual(s2["migrated"], 0)
        self.assertEqual(s2["unlink_legacy"], 0)
        # The new layout file is preserved across re-runs.
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "alpha.json").exists(),
        )

    def test_slugs_filter(self) -> None:
        """The --slugs flag narrows the scan set per-board."""
        self._board("greenhouse")
        for slug in ("alpha", "bravo", "charlie"):
            self._write_legacy(
                "greenhouse", slug,
                _make_ok_profile(slug=slug, board="greenhouse",
                                 posting_cadence="weekly"),
            )

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter={"alpha", "bravo"},
            apply=True, include_meta=False,
        )
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["migrated"], 2)
        # Charlie survives at top-level because filter excluded it.
        self.assertTrue((eom.PROFILE_DIR / "greenhouse" / "charlie.json").exists())
        self.assertFalse(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "charlie.json").exists(),
        )

    def test_unreadable_legacy_file_counted_as_error(self) -> None:
        """A legacy file with malformed JSON is logged + counted, the
        loop continues, and the file is left untouched (so a future
        fix-then-rerun can recover it)."""
        self._board("greenhouse")
        bad_path = eom.PROFILE_DIR / "greenhouse" / "broken.json"
        with open(bad_path, "w") as f:
            f.write("{not valid json at all")
        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False,
        )
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["json_error"], 1)
        self.assertEqual(summary["migrated"], 0)
        self.assertTrue(bad_path.exists())  # not unlinked

    def test_no_legacy_files_short_circuits_cleanly(self) -> None:
        """Empty board → summary zeros, no errors, no crash."""
        self._board("greenhouse")
        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False,
        )
        self.assertEqual(summary, {
            "scanned": 0, "migrated": 0, "skipped_existing": 0,
            "unlink_legacy": 0, "unlink_drift": 0, "json_error": 0,
            "meta_renamed": 0,
        })

    def test_drift_unlinks_stale_cadence_copy(self) -> None:
        """A pre-existing cadence/daily copy at the wrong bucket is
        treated as drift and unlinked; legacy top-level + new cadence/weekly
        + drift gone."""
        self._board("greenhouse")
        payload = _make_ok_profile(
            slug="acme", board="greenhouse", posting_cadence="weekly",
        )
        self._write_legacy("greenhouse", "acme", payload)
        self._write_partial_state_file(
            "greenhouse", "cadence/daily", "acme", payload,
        )

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False,
        )
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["unlink_legacy"], 1)
        self.assertEqual(summary["unlink_drift"], 1)
        self.assertFalse(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "daily" / "acme.json").exists(),
        )
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "weekly" / "acme.json").exists(),
        )

    def test_meta_file_disabled_via_no_include_meta(self) -> None:
        """``--no-include-meta`` keeps the legacy _skip_list.json file
        in place (operator still wants it for some reason)."""
        board = self._board("greenhouse")
        with open(board / "_skip_list.json", "w") as f:
            json.dump({"schema_version": 1, "board": "greenhouse",
                       "computed_at": "x", "slugs": []}, f)
        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False,
        )
        self.assertEqual(summary["meta_renamed"], 0)
        self.assertTrue((board / "_skip_list.json").exists())
        self.assertFalse((board / "_skip_list.deprecated.json").exists())

    def test_meta_deprecated_already_present_uses_numeric_suffix(self) -> None:
        """If _skip_list.deprecated.json already exists, the migrator
        finds a free numeric suffix rather than clobbering."""
        board = self._board("greenhouse")
        with open(board / "_skip_list.json", "w") as f:
            json.dump({"schema_version": 1, "board": "greenhouse",
                       "computed_at": "x", "slugs": []}, f)
        with open(board / "_skip_list.deprecated.json", "w") as f:
            json.dump({"schema_version": 1, "board": "greenhouse",
                       "computed_at": "earlier-run", "slugs": []}, f)

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=True,
        )
        self.assertEqual(summary["meta_renamed"], 1)
        self.assertTrue((board / "_skip_list.json").exists() is False)
        # Original deprecated copy preserved.
        self.assertTrue((board / "_skip_list.deprecated.json").exists())
        # New deprecated copy has numeric suffix.
        self.assertTrue((board / "_skip_list.deprecated.1.json").exists())


    def test_drift_in_errors_dir_re_routes_to_cadence_rare(self) -> None:
        """End-to-end auto-clean: a pre-existing misroute at
        ``errors/<slug>.json`` (status=skipped + reason=fewer_than_3_jobs +
        source_jobs_count=1) gets re-discovered by the drift-repair
        pass and MOVED to ``cadence/rare/<slug>.json``. The original
        errors/ copy is unlinked via unlink_drift. This is the
        production scenario after the broken pre-patch migration
        run \u2014 the discovered misrouted files alone are enough to
        repair without any legacy top-level files surviving."""
        board = self._board("greenhouse")
        err_dir = board / "errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        payload = _make_failed_envelope(
            slug="ardea-partners", board="greenhouse",
            status="skipped", reason="fewer_than_3_jobs",
            source_jobs_count=1,
        )
        bad_path = err_dir / "ardea-partners.json"
        with open(bad_path, "w") as f:
            json.dump(payload, f)
        # No legacy top-level file \u2014 simulating "post-broken-migration"
        # state where every legacy file has already been unlinked.

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False,
        )
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["unlink_drift"], 1)
        self.assertEqual(summary["unlink_legacy"], 0)
        self.assertEqual(summary["json_error"], 0)
        self.assertEqual(summary["skipped_existing"], 0)

        # New location: cadence/rare/<slug>.json
        self.assertTrue(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "rare" / "ardea-partners.json").exists(),
        )
        # Misroute cleaned up.
        self.assertFalse(bad_path.exists())

    def test_drift_pass_skips_legit_failure_in_errors(self) -> None:
        """A real status=failed envelope in errors/<slug>.json is NOT
        misrouted out of errors/ \u2014 the classifier returns the same
        errors/<slug>.json path, planner sees target_path.exists() and
        fires skip_existing (no I/O, counter bump only)."""
        board = self._board("greenhouse")
        err_dir = board / "errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        payload = _make_failed_envelope(
            slug="1inch", board="greenhouse", status="failed",
            reason="fetch returned empty or errored (transient)",
        )
        legit_path = err_dir / "1inch.json"
        with open(legit_path, "w") as f:
            json.dump(payload, f)

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False,
        )
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["skipped_existing"], 1)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(summary["unlink_drift"], 0)
        # File still in errors/ \u2014 untouched.
        self.assertTrue(legit_path.exists())

    def test_no_repair_drift_flag_skips_drift_pass(self) -> None:
        """``repair_drift=False`` means errors/<slug>.json is NOT
        re-discovered, even when a misroute is present. Legacy
        top-level pass still runs normally."""
        board = self._board("greenhouse")
        # Pre-populate a misroute AND a legacy top-level OK profile.
        err_dir = board / "errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        bad_payload = _make_failed_envelope(
            slug="misrouted", board="greenhouse",
            status="skipped", reason="fewer_than_3_jobs",
            source_jobs_count=1,
        )
        with open(err_dir / "misrouted.json", "w") as f:
            json.dump(bad_payload, f)
        self._write_legacy(
            "greenhouse", "alpha",
            _make_ok_profile(slug="alpha", board="greenhouse",
                             posting_cadence="weekly"),
        )

        summary = mlm._migrate_board(
            board="greenhouse", slugs_filter=None, apply=True,
            include_meta=False, repair_drift=False,
        )
        # Only the legacy file was processed.
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["unlink_legacy"], 1)
        # Misroute in errors/ untouched because the drift pass was disabled.
        self.assertTrue((err_dir / "misrouted.json").exists())
        self.assertFalse(
            (eom.PROFILE_DIR / "greenhouse" / "cadence" / "rare" / "misrouted.json").exists(),
        )


# ---------------------------------------------------------------------------
# Discovery helper — meta files excluded
# ---------------------------------------------------------------------------
class TestDiscoverLegacyTopLevelPaths(_MigrationTestBase):
    def test_meta_files_excluded(self) -> None:
        """``_skip_list.json`` and any ``_*.json`` file at the board root
        are NOT returned by the file-discovery helper."""
        board = self._board("greenhouse")
        # Real profile file.
        self._write_legacy(
            "greenhouse", "alpha",
            _make_ok_profile(slug="alpha", board="greenhouse",
                             posting_cadence="weekly"),
        )
        # Meta file (must NOT appear in the discovery result).
        with open(board / "_skip_list.json", "w") as f:
            json.dump({"slugs": []}, f)

        paths = mlm._discover_legacy_top_level_paths("greenhouse")
        self.assertEqual([p.name for p in paths], ["alpha.json"])

    def test_missing_board_dir_returns_empty(self) -> None:
        """No enriched/<board>/ dir at all → empty list, no crash."""
        paths = mlm._discover_legacy_top_level_paths("lever")
        self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
