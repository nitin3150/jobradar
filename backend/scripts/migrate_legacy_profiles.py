"""One-shot migrator: legacy top-level profiles → per-cadence layout.

Why this exists
===============

The boards-runner enrichment script originally wrote every (board, slug)
profile as a flat top-level file at
``data/enriched/<board>/<slug>.json`` plus a single ``_skip_list.json``
meta-file listing the slugs it classified as never-scannable. The
boards runner also consumed that meta file via
``BOARDS_USE_ENRICHED_PROFILES=1``.

When the enrichment pipeline moved to a per-cadence directory layout
(``data/enriched/<board>/{cadence/<bucket>|skip|errors}/<slug>.json``)
the runner's gate became ``BOARDS_CADENCES=<csv>`` — read directly from
filenames, no meta-file. That left the on-disk state desynced from the
runner contract:

  * 7,000+ legacy top-level files invisible to every cron tier
    (active/dormant/probe only read ``cadence/<bucket>/<slug>.json``).
  * ``_skip_list.json`` files at each board root that no code path
    reads anymore.

This script bridges the two layouts — one-to-one file moves, no LLM
calls, no fetches — so everything the LLM has already classified is
visible to the per-cadence scheduler on the next tick.

Output layout (per board, post-migration)
----------------------------------------

    data/enriched/<board>/cadence/<bucket>/<slug>.json
        # status="ok" profiles, re-bucketed from posting_cadence
        # via the current _bucket_for_ok_profile helper. Also covers
        # v2 OrgProfile-shaped payloads WITHOUT the ``status: "ok"``
        # stamp (older enrichment-script versions) — the migration
        # stamps the status field on the way out so the on-disk file
        # is consistent with the current wire contract.
    data/enriched/<board>/cadence/rare/<slug>.json
        # status="skipped" envelopes with reason "fewer_than_<N>_jobs"
        # AND source_jobs_count > 0 — small-but-real orgs the
        # enrichment script never classified (too few jobs for the
        # LLM to bother with). These files are ENVELOPE-SHAPED
        # (not OrgProfile-shaped); they're slug-discovery tokens for
        # the weekly probe workflow. The next LLM re-enrich will
        # overwrite them with a proper OrgProfile payload.
    data/enriched/<board>/skip/<slug>.json
        # status="ok" profiles that the CURRENT _compute_skip_for_profile
        # rules (Rule 1 sponsor-block / clearance-required, Rule 2
        # confidently non-tech) classify as never-scannable. Re-application
        # is intentional — Rule 1 (dead/rare+stale) was REMOVED from the
        # skip rules; an org the OLD skip_meta-file classified as skip
        # because of Rule 1 naturally re-routes to cadence/dead/ so the
        # weekly probe picks it up.
    data/enriched/<board>/errors/<slug>.json
        # status="failed" envelopes OR status="skipped" envelopes
        # whose reason is NOT ``fewer_than_*`` (transient fetches,
        # parse errors). These are the on-disk shape written by
        # :func:`scripts.enrich_org_profiles._write_status_envelope`.
    data/enriched/<board>/_skip_list.deprecated.json
        # the meta file, renamed for audit. The boards runner never reads
        # it (filenames only) so renaming leaves the runner untouched.

Re-application of skip rules is intentional
-------------------------------------------

A legacy ``status: ok`` profile lands in ``skip/`` only if the CURRENT
``_bucket_for_ok_profile`` helper returns ``"skip"``. Because the
helper internally calls ``_compute_skip_for_profile`` which uses
``profile.get("status") != "ok"`` as a guard rail, calling it on a
``status: ok`` payload is safe and is the same code path the enrichment
script uses.

Skip semantics, summarized:

  * Status==ok + SponsorClosed OR ClearanceRequired → ``skip/``
  * Status==ok + ConfidentlyNonTech (tech_ratio < 15% AND confidence > 70%) → ``skip/``
  * Status==ok + everything else → ``cadence/<posting_cadence>/``
    (with ``unknown`` coerced via ``_bucket_for_ok_profile`` for LLM drift)

Atomicity
---------

Write-then-unlink. ``_atomic_write_json`` (POSIX atomic replace via
``os.replace``) writes the target path first; only AFTER the write
succeeds do we unlink the legacy source. A write failure leaves the
source intact, so a re-run of the script is idempotent on partial
state.

Schema-version normalization
----------------------------

Every migrated file gets ``schema_version`` upgraded to
``SCHEMA_VERSION`` (2) unconditionally. v1↔v2 wire-shape diff is just
the ``source_jobs[].description`` cap (4000 vs 600) — already past for
the 2026-07-14 sweep — and the on-disk body bytes are stable. The
stamp signals "this file was placed by the current-gen migrator".

CLI
---

::

    # Dry-run (default) — print plan only, no I/O.
    python scripts/migrate_legacy_profiles.py --board all

    # Apply (commit).
    python scripts/migrate_legacy_profiles.py --board all --apply

    # One board, specific slugs (sanity check).
    python scripts/migrate_legacy_profiles.py --board greenhouse \\
        --slugs stripe,vts --apply

    # Skip the meta-file rename but still migrate profiles.
    python scripts/migrate_legacy_profiles.py --board all \\
        --no-include-meta --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ``scripts/X.py`` boot path — make ``backend/`` importable so
# ``from scripts.enrich_org_profiles import ...`` resolves, matching
# the pattern in ``scripts/boards_scan.py`` and the enrichment script.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.enrich_org_profiles import (  # noqa: E402
    PROFILE_DIR,
    SCHEMA_VERSION,
    _atomic_write_json,
    _bucket_for_ok_profile,
    _stale_profile_paths_for_slug,
    _target_path_for_slug,
)

logger = logging.getLogger("jobradar.migrate")


# ---------------------------------------------------------------------------
# Target classification
# ---------------------------------------------------------------------------
# Reasons the enrichment script writes under ``status: "skipped"`` that
# route to ``cadence/rare/`` (small-but-real orgs) vs ``errors/`` (every
# other kind of skip — transient fetches, parse errors, etc.). The
# full reason string is ``f"fewer_than_{MIN_JOBS_FOR_LLM}_jobs"`` per
# :func:`scripts.enrich_org_profiles._write_status_envelope`'s call
# site; a ``startswith`` check is robust against future ``MIN_JOBS``
# changes without code changes here.
_SKIP_REASON_FEWER_THAN_PREFIX = "fewer_than_"


def _classify_migration_target(
    payload: dict,
    *,
    board: str,
    slug: str,
) -> tuple[Path, dict[str, Any], str]:
    """Compute the destination path for a legacy profile payload.

    Returns ``(target_path, normalized_payload, label)`` where ``label``
    is a human-readable summary used in the dry-run / apply log line.

    Routing rules (priority order; first match wins):

    * ``status == "ok"`` → ``_bucket_for_ok_profile`` decides between
      ``skip/<slug>.json`` (Rule 1 sponsor/clearance or Rule 2
      non-tech matched) and ``cadence/<bucket>/<slug>.json`` (one
      bucket from :data:`CADENCE_BUCKETS`).
    * ``status == "skipped"`` AND ``reason`` starts with
      ``fewer_than_`` AND ``source_jobs_count > 0`` →
      ``cadence/rare/<slug>.json``. The enrichment script writes
      ``status: "skipped"`` envelopes for orgs whose fetch returned
      fewer than ``MIN_JOBS_FOR_LLM`` jobs (default 3); these are
      small-but-real orgs that legitimately belong in the rare-cadence
      bucket so the weekly probe picks them up. Other skip reasons
      (transient fetches, malformed JSON, etc. → ``errors/``).
    * ``status == "failed"`` → ``errors/<slug>.json``.
    * ``status`` absent (None) AND shape is OK (``posting_cadence`` is
      set AND ``source_jobs_count > 0``) → coerce ``status = "ok"``
      and route via ``_bucket_for_ok_profile``. Handles v2
      OrgProfile-shaped payloads written WITHOUT the ``status: "ok"``
      stamp (older enrichment-script versions) — without this branch
      they'd be punted to ``errors/`` and the boards runner would
      never see them. Coercing to ``"ok"`` lets the current skip
      rules fire on rule-1 / rule-2 violations.
    * Anything else (unknown status string, missing cadence + no jobs,
      partial-shape payloads) → ``errors/<slug>.json``, counted in
      the action summary as ``unknown_status``.

    ``normalized_payload`` is a shallow copy of ``payload`` with
    ``schema_version`` rewritten to :data:`SCHEMA_VERSION` (2) so the
    on-disk file is in lock-step with the current wire contract.
    """
    out: dict[str, Any] = dict(payload)
    out["schema_version"] = SCHEMA_VERSION  # normalize unconditionally

    status = payload.get("status")

    # Case A: status=ok → bucket via _bucket_for_ok_profile.
    if status == "ok":
        bucket = _bucket_for_ok_profile(out)
        target_path = _target_path_for_slug(
            board=board, slug=slug, bucket=bucket,
        )
        return target_path, out, f"ok → {bucket}/{slug}.json"

    # Case B: status=skipped + fewer_than_N_jobs + real job data →
    # cadence/rare (small-but-real orgs the enrichment script skipped
    # because they had ≤ 2 jobs).
    #
    # NOTE: the destination file is ENVELOPE-SHAPED (status, reason,
    # source_jobs_count) rather than OrgProfile-shaped. The boards
    # runner reads file stems ("slugs") per :data:`CADENCE_BUCKETS`
    # — it does NOT Pydantic-parse the body of cadence/*/<slug>.json
    # at scan time. These rare-bucket files are slug-discovery
    # tokens for the weekly probe; the next LLM re-enrich rewrites
    # them with a proper OrgProfile payload, at which point they
    # become useful as full classification metadata for the runner.
    # Operators reading the on-disk JSON in the meantime see the
    # envelope shape and know "rare cadence — wait for re-enrich."
    if status == "skipped":
        reason = payload.get("reason") or ""
        source_jobs_count = payload.get("source_jobs_count") or 0
        if (
            reason.startswith(_SKIP_REASON_FEWER_THAN_PREFIX)
            and source_jobs_count > 0
        ):
            target_path = _target_path_for_slug(
                board=board, slug=slug, bucket="rare",
            )
            return (
                target_path,
                out,
                f"skipped (fewer_than) → cadence/rare/{slug}.json",
            )
        # Other skip reasons stay in errors/ (transient fetches,
        # parse errors, etc.).
        target_path = PROFILE_DIR / board / "errors" / f"{slug}.json"
        return target_path, out, f"skipped → errors/{slug}.json"

    # Case C: status=failed → errors/.
    if status == "failed":
        target_path = PROFILE_DIR / board / "errors" / f"{slug}.json"
        return target_path, out, f"failed → errors/{slug}.json"

    # Case D: no status field but shape is OK (v2 OrgProfile-shaped
    # payloads written without the status stamp). Stamp status="ok"
    # so the current skip rules fire — otherwise Rule 1 sponsor / DOD
    # clearance + Rule 2 non-tech violations with missing status
    # would route to cadence/unknown/ instead of skip/.
    if status is None:
        cadence = payload.get("posting_cadence")
        source_jobs_count = payload.get("source_jobs_count") or 0
        if cadence and source_jobs_count > 0:
            out["status"] = "ok"
            bucket = _bucket_for_ok_profile(out)
            target_path = _target_path_for_slug(
                board=board, slug=slug, bucket=bucket,
            )
            return target_path, out, f"shape-ok → {bucket}/{slug}.json"

    # Default: errors/ (unknown — preserve for operator inspection).
    target_path = PROFILE_DIR / board / "errors" / f"{slug}.json"
    return (
        target_path,
        out,
        f"unknown_status={status!r} → errors/{slug}.json",
    )


# ---------------------------------------------------------------------------
# Per-file migration plan
# ---------------------------------------------------------------------------
def _plan_actions_for_slug(
    *,
    board: str,
    slug: str,
    payload: dict,
) -> list[tuple[str, Path, dict[str, Any] | None]]:
    """Return the ordered list of (kind, path, body) actions to take
    for a single legacy top-level file.

    The plan is the SAME for dry-run and apply modes — only the I/O
    gating at execution time differs (``_execute_actions(apply=...)``
    is what gates writes/unlinks). Plan-only separation lets the
    operator preview a full migration before committing.

    Action kinds:

    * ``("write", target_path, normalized_payload)`` — write the
      payload to ``target_path`` if it doesn't already exist.
    * ``("skip_existing", target_path, None)`` — ``target_path`` already
      exists, trust it. Do NOT rewrite.
    * ``("unlink_legacy", legacy_path, None)`` — unlink the legacy
      top-level file. Always executed on apply (it's the file we moved).
    * ``("unlink_drift", stale_path, None)`` — unlink a stale duplicate
      (e.g. the legacy copy that lived in ``cadence/<wrong-bucket>/``).
      Logged as a warning because the on-disk state had partial-migration
      drift before this run.
    """
    target_path, normalized, _label = _classify_migration_target(
        payload, board=board, slug=slug,
    )
    actions: list[tuple[str, Path, dict[str, Any] | None]] = []

    if target_path.exists():
        actions.append(("skip_existing", target_path, None))
    else:
        actions.append(("write", target_path, normalized))

    # Walk the full stale-pathset for the slug, except target.
    stale_paths = [
        p for p in _stale_profile_paths_for_slug(board=board, slug=slug)
        if p != target_path
    ]
    legacy_path = PROFILE_DIR / board / f"{slug}.json"
    for sp in stale_paths:
        if not sp.exists():
            continue
        if sp == legacy_path:
            actions.append(("unlink_legacy", sp, None))
        else:
            actions.append(("unlink_drift", sp, None))

    return actions


# ---------------------------------------------------------------------------
# Per-board drive
# ---------------------------------------------------------------------------
def _discover_legacy_top_level_paths(board: str) -> list[Path]:
    """Return every legacy top-level ``*.json`` under
    ``data/enriched/<board>/``, excluding meta files (``_skip_list.json``
    and any future ``_*``).
    """
    board_dir = PROFILE_DIR / board
    if not board_dir.exists():
        return []
    return sorted(
        p for p in board_dir.glob("*.json")
        if not p.name.startswith("_")
    )


def _discover_drift_candidates(board: str) -> list[Path]:
    """Return every ``errors/<slug>.json`` under
    ``data/enriched/<board>/errors/``.

    These are files that a PREVIOUS classifier pass may have
    misrouted \u2014 e.g. ``status="skipped"`` envelopes with
    ``reason="fewer_than_<N>_jobs"`` copied into ``errors/`` when
    they should have landed in ``cadence/rare/``, or shape-OK
    no-status OrgProfiles dumped into ``errors/`` as
    ``unknown_status`` when they belong in a cadence bucket. The
    two-pass migration in :func:`_migrate_board` re-feeds each
    candidate through the current :func:`_classify_migration_target`
    so the planner naturally fires ``write + unlink_drift`` on
    correctly-routed files (``target`` differs from source) or a
    ``skip_existing`` no-op for files already at their correct
    location (e.g. legitimate ``status="failed"`` envelopes which
    legitimately belong in ``errors/``).

    Excludes meta files (``_*.json``) defensively even though
    ``errors/`` is not expected to contain any.
    """
    err_dir = PROFILE_DIR / board / "errors"
    if not err_dir.exists():
        return []
    return sorted(
        p for p in err_dir.glob("*.json")
        if not p.name.startswith("_")
    )


def _migrate_board(
    *,
    board: str,
    slugs_filter: set[str] | None,
    apply: bool,
    include_meta: bool,
    repair_drift: bool = True,
) -> dict[str, int]:
    """Migrate one board's worth of files. Two passes:

    1. **Legacy pass** — top-level ``<slug>.json`` files below
       ``data/enriched/<board>/`` (the legacy v1/v2 layout). These
       produce ``write + unlink_legacy`` when the target differs
       from the source.
    2. **Drift pass** (optional, default on) — ``errors/<slug>.json``
       files. Re-feeds each through the current classifier so any
       pre-existing misrouted envelope (e.g. ``status="skipped"`` +
       ``reason="fewer_than_3_jobs"`` copied into ``errors/`` by a
       previous buggy migration run) gets repaired to its correct
       bucket. Produces ``write + unlink_drift`` for misroutes and
       ``skip_existing`` for legitimately-placed files like real
       ``status="failed"`` envelopes.

    The drift pass intentionally runs AFTER the legacy pass so a
    slug that has BOTH a legacy top-level file AND an ``errors/``
    copy (partial-state scenario) gets the legacy pass first \u2014 the
    legacy classify then unlinks the ``errors/`` copy as ``unlink_``
    ``drift`` during the stale-paths walk, and the drift pass
    subsequently sees an empty / non-existent path and skips.

    Returns a ``{action_kind: count}`` summary.
    """
    summary = {
        "scanned": 0,
        "migrated": 0,
        "skipped_existing": 0,
        "unlink_legacy": 0,
        "unlink_drift": 0,
        "json_error": 0,
        "meta_renamed": 0,
    }

    legacy_paths = _discover_legacy_top_level_paths(board)
    drift_paths = (
        _discover_drift_candidates(board) if repair_drift else []
    )
    if not legacy_paths and not drift_paths and not include_meta:
        print(
            f"[migrate] {board}: 0 legacy + 0 drift files, skipping",
            flush=True,
        )
        return summary

    print(
        f"[migrate] {board}: {len(legacy_paths)} legacy + "
        f"{len(drift_paths)} drift files "
        f"({'APPLY' if apply else 'dry-run'})",
        flush=True,
    )

    # Pass 1: legacy top-level. Pass 2: errors/ drift repair. Legacy
    # takes priority so a partial-state slug (legacy + errors/ both
    # present) is correctly unlinked via the legacy pass's
    # unlink_drift walk before the drift pass sees (and skips) the
    # now-empty errors/<slug>.json.
    candidates: list[tuple[str, Path]] = (
        [("legacy", p) for p in legacy_paths]
        + [("drift", p) for p in drift_paths]
    )
    for _pass_name, path in candidates:
        # Case 6 defense: the legacy pass may have unlinked this drift
        # file via its own unlink_drift walk before the drift pass
        # gets to it. Skip silently \u2014 not an error.
        if not path.exists():
            continue
        slug = path.stem
        if slugs_filter and slug not in slugs_filter:
            continue
        summary["scanned"] += 1

        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "unreadable JSON at %s (%s)",
                path.relative_to(PROFILE_DIR), exc,
            )
            summary["json_error"] += 1
            continue

        try:
            actions = _plan_actions_for_slug(
                board=board, slug=slug, payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, log+continue
            logger.warning(
                "classify failed for %s/%s: %s",
                board, slug, exc,
            )
            continue

        _execute_actions(
            board=board, slug=slug, actions=actions,
            apply=apply, summary=summary,
        )

    # Meta-file rename. Done LAST so a partial migration that crashes
    # earlier leaves the meta file intact for operator inspection.
    if include_meta:
        summary["meta_renamed"] = _deprecate_meta_file(
            board=board, apply=apply,
        )

    return summary


def _execute_actions(
    *,
    board: str,
    slug: str,
    actions: list[tuple[str, Path, dict[str, Any] | None]],
    apply: bool,
    summary: dict[str, int],
) -> None:
    """Iterate the action list, executing ``write``/``unlink_legacy``/
    ``unlink_drift`` actions only when ``apply=True``.

    Dry-run (``apply=False``) prints the planned action but performs
    no filesystem mutation — that's how an operator previews the
    migration before committing. ``skip_existing`` is metadata-only
    either way (just a log line and a counter bump).

    Actions are wrapped so a single action's failure doesn't abort the
    whole slug's plan — log + continue. ``write`` failures DO leave the
    legacy source untouched because of the write-then-unlink ordering.

    Summary counters reflect the *plan* (what would happen or what did
    happen), not just the writes that ran — so dry-run output and
    apply output line up for the operator's at-a-glance comparison.
    """
    for kind, path, body in actions:
        rel = path.relative_to(PROFILE_DIR)
        try:
            if kind == "write":
                assert body is not None
                if apply:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write_json(path, body)
                    print(
                        f"[migrate] write   {board}/{slug}: {rel}",
                        flush=True,
                    )
                else:
                    print(
                        f"[migrate] (dry)   {board}/{slug}: would write {rel}",
                        flush=True,
                    )
                summary["migrated"] += 1
            elif kind == "skip_existing":
                print(
                    f"[migrate] skip    {board}/{slug}: target already at {rel} "
                    f"(on-disk pre-migration copy trusted)",
                    flush=True,
                )
                summary["skipped_existing"] += 1
            elif kind == "unlink_legacy":
                if apply:
                    path.unlink()
                    print(
                        f"[migrate] unlink  {board}/{slug}: legacy {rel}",
                        flush=True,
                    )
                else:
                    print(
                        f"[migrate] (dry)   {board}/{slug}: would unlink legacy {rel}",
                        flush=True,
                    )
                summary["unlink_legacy"] += 1
            elif kind == "unlink_drift":
                # Drift warning fires in BOTH dry-run and apply modes
                # because the *detection* of partial-state drift is
                # mode-independent — the operator should see it whether
                # or not they committed. Single parameterized call so
                # the message has one source of truth and stays in
                # sync with the apply/dry branching below.
                verb = "unlinking now" if apply else "would unlink on apply"
                logger.warning(
                    "drift: %s/%s had stale duplicate at %s — %s",
                    board, slug, rel, verb,
                )
                if apply:
                    path.unlink()
                    print(
                        f"[migrate] drift   {board}/{slug}: {rel} unlinked",
                        flush=True,
                    )
                else:
                    print(
                        f"[migrate] (dry)   {board}/{slug}: would unlink drift {rel}",
                        flush=True,
                    )
                summary["unlink_drift"] += 1
            else:
                logger.error(
                    "unknown action kind %r for %s/%s — skipping",
                    kind, board, slug,
                )
        except OSError as exc:
            logger.warning(
                "%s failed for %s/%s path=%s: %s",
                kind, board, slug, rel, exc,
            )


def _deprecate_meta_file(*, board: str, apply: bool) -> int:
    """Rename ``data/enriched/<board>/_skip_list.json`` to
    ``_skip_list.deprecated.json`` so the operator can audit what the
    legacy skip-rule set classified as skip, while guaranteeing the
    runner's filename-only reader won't see it.

    Returns 1 if renamed, 0 if no meta file exists, or 1 with a logged
    note if the deprecated file already existed (no clobber).
    """
    board_dir = PROFILE_DIR / board
    legacy_meta = board_dir / "_skip_list.json"
    deprecated_meta = board_dir / "_skip_list.deprecated.json"
    if not legacy_meta.exists():
        return 0
    if deprecated_meta.exists():
        logger.info(
            "%s/_skip_list.deprecated.json already exists — leaving it "
            "and adding legacy as _skip_list.deprecated.NN.json",
            board,
        )
        # Find a free numeric suffix.
        n = 1
        while (board_dir / f"_skip_list.deprecated.{n}.json").exists():
            n += 1
        deprecated_meta = board_dir / f"_skip_list.deprecated.{n}.json"

    if apply:
        os.replace(legacy_meta, deprecated_meta)
        print(
            f"[migrate] meta    {board}: {_skip_list_path(legacy_meta)} → "
            f"{deprecated_meta.name}",
            flush=True,
        )
    else:
        print(
            f"[migrate] meta    {board}: (dry) would rename "
            f"{legacy_meta.name} → {deprecated_meta.name}",
            flush=True,
        )
    return 1


def _skip_list_path(p: Path) -> str:
    """Render ``p`` relative to PROFILE_DIR if possible, else str(p)."""
    try:
        return str(p.relative_to(PROFILE_DIR))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    p.add_argument(
        "--board",
        choices=["all", "ashby", "greenhouse", "lever"],
        default="all",
        help="Which board to migrate. Default: all.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Commit the moves + meta-file rename. Without --apply the "
            "script is dry-run (prints plan, performs no I/O)."
        ),
    )
    p.add_argument(
        "--slugs",
        default=None,
        help=(
            "Comma-separated slug subset to migrate. Filters the legacy "
            "top-level file set per-board; combined with --board if set."
        ),
    )
    p.add_argument(
        "--include-meta",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Rename _skip_list.json → _skip_list.deprecated.json after "
            "the file migrations complete. Default: True. Use "
            "--no-include-meta to keep the meta file intact."
        ),
    )
    p.add_argument(
        "--repair-drift",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "After the legacy top-level pass, ALSO scan errors/<slug>.json "
            "files and re-classify each through the current classifier. "
            "Misrouted envelopes (e.g. status=skipped + reason=fewer_than_* "
            "copied into errors/ by a previous buggy run) get repaired to "
            "their correct cadence/skip location; legitimate status=failed "
            "envelopes are a skip_existing no-op. Default: True. Use "
            "--no-repair-drift to limit the migration to legacy top-level "
            "files only (the v1.x behavior)."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="[migrate] %(asctime)s %(levelname)s %(message)s",
    )

    slugs_filter: set[str] | None = None
    if args.slugs:
        slugs_filter = {s.strip() for s in args.slugs.split(",") if s.strip()}

    boards = ["ashby", "greenhouse", "lever"] if args.board == "all" else [args.board]
    summaries: dict[str, dict[str, int]] = {}
    for board in boards:
        summaries[board] = _migrate_board(
            board=board,
            slugs_filter=slugs_filter,
            apply=args.apply,
            include_meta=args.include_meta,
            repair_drift=args.repair_drift,
        )

    # Operator at-a-glance summary.
    total_migrated = 0
    total_skipped = 0
    total_drift = 0
    total_errors = 0
    for board, s in summaries.items():
        print(
            f"[migrate] summary {board}: "
            f"scanned={s['scanned']} migrated={s['migrated']} "
            f"skipped_existing={s['skipped_existing']} "
            f"unlink_legacy={s['unlink_legacy']} "
            f"unlink_drift={s['unlink_drift']} "
            f"json_error={s['json_error']} "
            f"meta_renamed={s['meta_renamed']}",
            flush=True,
        )
        total_migrated += s["migrated"]
        total_skipped += s["skipped_existing"]
        total_drift += s["unlink_drift"]
        total_errors += s["json_error"]

    print(
        f"[migrate] totals: migrated={total_migrated} "
        f"skipped_existing={total_skipped} drift={total_drift} "
        f"errors={total_errors}",
        flush=True,
    )

    if not args.apply:
        print(
            "[migrate] DRY RUN. Re-run with --apply to commit. Caveat: "
            "the source filesystem is unchanged; review the summary "
            "lines above for board-level counts.",
            flush=True,
        )
        return 0

    print(
        "[migrate] DONE. The next GHA cron tick (active, dormant, probe) "
        "will scan the migrated cadence/skip/errors layout. Old "
        "top-level files have been unlinked and "
        "_skip_list.json → _skip_list.deprecated.json.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
