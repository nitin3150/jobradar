import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import re

LOGGER_NAME = "jobradar.runner"

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pipeline.nodes.jobs_boards.ashby import fetch as ashby_fetch
from pipeline.nodes.jobs_boards.greenhouse import fetch as greenhouse_fetch
from pipeline.nodes.jobs_boards.lever import fetch as lever_fetch
# Read the operator's seniority band from the Preferences singleton.
# Same coupling pattern :mod:`services.scoring_service` already uses to
# pull ``job_fit_threshold`` — keeps the singleton the single source of
# truth without leaking route-layer modules into the runner.
from routes.settings import _PREFS_STATE
from utils.filters import (
    bench_org_from_text,
    filter_roles,
    is_relevant_role,
    min_years_required,
)
from utils.http import build_client
from utils.seen import load_file, save_seen
from utils.time_check import parse_published_at

DATA_DIR = BACKEND_ROOT / "data"
ORG_INDEX = {
    "ashby": (DATA_DIR / "ashby_companies.json", ashby_fetch),
    "greenhouse": (DATA_DIR / "greenhouse_companies.json", greenhouse_fetch),
    "lever": (DATA_DIR / "lever_companies.json", lever_fetch),
}

# An active org must fail this many consecutive runs before being benched, so a
# transient 404 (maintenance / rate limit) doesn't permanently drop coverage.
MISSING_THRESHOLD = 3
MAX_WORKERS = 8

# Years-of-experience floor for the role-drop gate: any posting that
# hard-requires ≥ this many years is dropped before it reaches the
# LLM scorer. The operator explicitly wants 6+ years roles
# discarded. We do NOT bench the whole org on a 6+-years match —
# only on the harder citizenship-required / hard sponsorship-block
# match (see the loop in ``run_all`` and
# :func:`utils.filters.bench_org_from_text`).
MIN_YEARS_FLOOR_DROP = 6

# ``main()``'s argparse ``--delta-hours`` default when no
# ``BOARDS_DELTA_HOURS`` env var is exported: 1h (the historical
# CLI default, preserved so cron scripts that don't know about the
# env var keep their existing behavior). When the env var IS set,
# ``main()``'s default flips to ``DEFAULT_DELTA_HOURS`` instead —
# see ``main()``'s comment block for the conditional.
CLI_DELTA_HOURS_WHEN_ENV_UNSET = 1

# Default boards-window when ``run_all(...)`` is called without an explicit
# ``delta_hours``: 168 hours = 1 week (matches the cadence the LangGraph
# per-domain scheduler uses for funding/OSS and the explicit ``discover``
# route hands to the runner). Operators can override at deployment time
# by exporting ``BOARDS_DELTA_HOURS=N`` in ``.env``; the constant is
# read at *module-import* time (the same convention as ``GITHUB_TOKEN``
# in ``pipeline.nodes.oss.github_issues``), so a worker restart is
# required for a change to take effect.
#
# See ``CLI_DELTA_HOURS_WHEN_ENV_UNSET`` for the conditional CLI
# fallback (``main()``) that pairs with this env-driven default.
#
# We ``SystemExit`` on a malformed value rather than letting the
# interpreter raise a cryptic ``ValueError`` — operators reading
# worker logs at boot see a single actionable line instead of a
# traceback going to a stdlib int() conversion. Non-positive values
# are rejected too (a negative or zero lookback would make
# ``compute_since_cutoff`` yield a *future* timestamp, defeating
# the per-org ``since`` filter).
_DEFAULT_DELTA_HOURS_FALLBACK = "168"
_raw_delta_hours = os.environ.get("BOARDS_DELTA_HOURS", _DEFAULT_DELTA_HOURS_FALLBACK)
try:
    DEFAULT_DELTA_HOURS = int(_raw_delta_hours)
    if DEFAULT_DELTA_HOURS < 1:
        raise ValueError(
            f"value must be >= 1 (got {DEFAULT_DELTA_HOURS}); a negative or "
            f"zero lookback would make compute_since_cutoff yield a future "
            f"timestamp, which would defeat the per-org fetch filter."
        )
except ValueError as exc:
    raise SystemExit(
        f"BOARDS_DELTA_HOURS={_raw_delta_hours!r} is not a valid positive integer: {exc}. "
        f"Expected a positive integer >= 1 (e.g. '24', '168', '720')."
    ) from exc


class UnknownBoardError(ValueError):
    pass


import logging  # noqa: E402  (kept after stdlib + third-party imports for readability)
logger = logging.getLogger(LOGGER_NAME)


def _text_hits_clearance(title: str, description: str) -> bool:
    """True when the combined title+description hits the existing
    :data:`utils.filters.CLEARANCE_PATTERNS` regex set.

    Re-imported lazily so editing the patterns module doesn't create a
    circular import at startup. ``is_relevant_role`` already filters
    on these patterns as a role-drop gate; we duplicate the check
    here so the boards runner can attribute a *failed* match to
    a specific (board, slug) pair and bench it without re-running
    the whole role filter.
    """
    from utils.filters import CLEARANCE_PATTERNS

    joined = f"{title or ''} {description or ''}".lower()
    if not joined.strip():
        return False
    return any(re.search(pat, joined) for pat in CLEARANCE_PATTERNS)


def compute_since_cutoff(now=None, delta_hours=1, last_run=None):
    now = now or datetime.now(timezone.utc)
    if last_run is not None:
        return max(last_run, now - timedelta(hours=delta_hours))
    return now - timedelta(hours=delta_hours)


def validate_boards(boards):
    unknown = [b for b in boards if b not in ORG_INDEX]
    if unknown:
        raise UnknownBoardError(f"unknown board(s): {unknown}; valid: {list(ORG_INDEX)}")
    return boards


def load_orgs(board_name):
    path = ORG_INDEX[board_name][0]
    with open(path, "r") as handle:
        orgs = json.load(handle)

    missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
    if missing_path.exists():
        with open(missing_path, "r") as handle:
            missing = set(json.load(handle))
        return [slug for slug in orgs if slug not in missing]
    return orgs


def load_last_run_state(path=None):
    path = path or DATA_DIR / "last_run.json"
    if not path.exists():
        return {}
    with open(path, "r") as handle:
        return json.load(handle)


def save_last_run_state(state, path=None):
    path = path or DATA_DIR / "last_run.json"
    with open(path, "w") as handle:
        json.dump(state, handle, indent=2)


def load_failure_counts(path=None):
    path = path or DATA_DIR / "missing_failures.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except Exception:
        return {}


def save_failure_counts(counts, path=None):
    path = path or DATA_DIR / "missing_failures.json"
    with open(path, "w") as handle:
        json.dump(counts, handle, indent=2)


def execute_fetch(fetcher, board_name, slug, since, seen_ids, client):
    """Run one org fetch and classify the outcome. Never mutates shared state."""
    try:
        result = fetcher(slug, client=client, since=since, seen_ids=seen_ids)
        return {"board": board_name, "slug": slug, "outcome": "ok", **result}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        outcome = "missing" if status_code in {404, 410} else "error"
        if outcome == "error":
            print(f"HTTP error while scraping {board_name}/{slug}: {exc}")
        return {"board": board_name, "slug": slug, "outcome": outcome, "jobs": [], "new_ids": {}, "latest": None}
    except httpx.TimeoutException:
        print(f"Request timed out while scraping {board_name}/{slug}")
        return {"board": board_name, "slug": slug, "outcome": "error", "jobs": [], "new_ids": {}, "latest": None}
    except Exception as exc:
        print(f"Scraper error for {board_name}/{slug}: {exc}")
        return {"board": board_name, "slug": slug, "outcome": "error", "jobs": [], "new_ids": {}, "latest": None}


def _write_missing_lists(boards, newly_missing, recovered):
    """Bench orgs over the failure threshold, un-bench any that recovered."""
    for board_name in boards:
        missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
        previous = set()
        if missing_path.exists():
            with open(missing_path, "r") as handle:
                previous = set(json.load(handle))
        combined = (previous | newly_missing.get(board_name, set())) - recovered.get(board_name, set())
        with open(missing_path, "w") as handle:
            json.dump(sorted(combined), handle, indent=2)


def run_all(delta_hours=DEFAULT_DELTA_HOURS, boards=None, limit=None):
    boards = validate_boards(boards or list(ORG_INDEX.keys()))
    seen = load_file()
    seen_ids = frozenset(seen.keys())  # read-only snapshot for worker threads
    failure_counts = load_failure_counts()

    last_run_state = load_last_run_state()
    last_run_timestamp = None
    if last_run_state.get("last_run"):
        last_run_timestamp = parse_published_at(last_run_state["last_run"])
    since = compute_since_cutoff(delta_hours=delta_hours, last_run=last_run_timestamp)

    results = []
    org_last_posted = {}
    newly_missing = {board: set() for board in boards}
    recovered = {board: set() for board in boards}
    # Clearance/6+-years gate attributes these to the underlying
    # (board, slug) so we can append them to <board>_missing_orgs.json
    # after the fetch loop closes. Two distinct triggers:
    #   - ``newly_cleared_or_seniority_blocked`` — boarder boards that
    #     hard-require citizenship, sponsor-block, or >=6 years
    #     experience per the operator's "bench the company" policy.
    #     These go into the missing-orgs list immediately (not
    #     throttled by MISSING_THRESHOLD — the operator's intent was
    #     explicit "any mention = remove the company").
    #   - ``newly_too_senior`` — roles dropped just because of >=6
    #     years but the company's other roles aren't necessarily
    #     blocked. We just drop them from results; no org-level bench.
    newly_cleared_or_seniority_blocked: dict[str, set[str]] = {
        board: set() for board in boards
    }

    client = build_client()
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = []
            for board_name in boards:
                orgs = load_orgs(board_name)
                if limit is not None:
                    orgs = orgs[:limit]
                fetcher = ORG_INDEX[board_name][1]
                for slug in orgs:
                    futures.append(
                        executor.submit(execute_fetch, fetcher, board_name, slug, since, seen_ids, client)
                    )

            # Merge in the main thread only -> no concurrent mutation of shared state.
            for future in as_completed(futures):
                r = future.result()
                board_name, slug, outcome = r["board"], r["slug"], r["outcome"]
                board_failures = failure_counts.setdefault(board_name, {})

                if outcome == "ok":
                    kept_after_gates: list[dict] = []
                    slug_bench_reasons: set[str] = set()
                    for job in r["jobs"]:
                        title = job.get("title") or ""
                        description = (
                            job.get("description") or job.get("content") or ""
                        )
                        # 6+ years → drop the role only (per operator
                        # request: "discard the roles"). We DO NOT
                        # bench the whole company on a 6+-years match —
                        # only on the harder citizenship-required match
                        # below, which is the operator's expressed
                        # intent for "remove the company".
                        years_floor = min_years_required(
                            f"{title} {description}"
                        )
                        if years_floor is not None and years_floor >= MIN_YEARS_FLOOR_DROP:
                            logger.debug(
                                "drop %s/%s: hard-requires %d+ years experience",
                                board_name, slug, years_floor,
                            )
                            continue
                        # Clearance / hard-sponsorship-block text on ANY
                        # job from this org → bench the whole org. The
                        # operator explicitly wants "the name of that
                        # company should be removed from the list of
                        # companies" — so a single citizenship-required
                        # mention is enough.
                        if (
                            _text_hits_clearance(title, description)
                            or bench_org_from_text(f"{title} {description}")
                        ):
                            slug_bench_reasons.add(
                                "clearance_or_citizenship_required"
                            )
                            logger.debug(
                                "bench %s/%s: org-level disqualifier "
                                "(clearance/citizenship block) found on a job",
                                board_name, slug,
                            )
                            continue
                        # Source published_at + source_updated_at from
                        # the board's payload so the downstream Pydantic
                        # ``Job`` shape carries them into the DB row.
                        # ``parse_published_at`` is the existing helper
                        # that already handles Greenhouse's updatedAt /
                        # Lever's createdAt / etc. graceful fallback.
                        published = job.get("published_at")
                        if published:
                            parsed = parse_published_at(published)
                            if parsed is not None:
                                job["posted_at"] = parsed
                        updated = job.get("updated_at")
                        if updated and updated != published:
                            parsed_updated = parse_published_at(updated)
                            if parsed_updated is not None:
                                job["source_updated_at"] = parsed_updated
                        kept_after_gates.append(job)
                    results.extend(kept_after_gates)
                    for job_id, stamp in r["new_ids"].items():
                        seen[job_id] = stamp
                    if r.get("latest"):
                        org_last_posted[slug] = r["latest"]
                    board_failures.pop(slug, None)  # reset failure streak
                    if slug_bench_reasons:
                        newly_cleared_or_seniority_blocked[board_name].add(slug)
                    recovered[board_name].add(slug)
                elif outcome == "missing":
                    board_failures[slug] = board_failures.get(slug, 0) + 1
                    if board_failures[slug] >= MISSING_THRESHOLD:
                        newly_missing[board_name].add(slug)
                # "error" -> transient; leave failure count untouched, don't bench
    finally:
        client.close()

    _write_missing_lists(boards, newly_missing, recovered)
    # Org benches triggered by clearance/citizenship text on a single
    # job merge into the same `<board>_missing_orgs.json` file the
    # failure-threshold path writes to. Two distinct write paths
    # into one persistent file — failure-threshold benches (3
    # consecutive 404/410s) and content-trigger benches (1 mention).
    # Both keys end up on the same exclusion list, so a single
    # ``load_orgs(board_name)`` lookup filters them all out on the
    # next cron tick. Merging here keeps the on-disk schema flat.
    for board_name in boards:
        disqualified = newly_cleared_or_seniority_blocked.get(board_name, set())
        if not disqualified:
            continue
        missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
        previous: set[str] = set()
        if missing_path.exists():
            with open(missing_path, "r") as handle:
                try:
                    previous = set(json.load(handle))
                except (json.JSONDecodeError, TypeError):
                    previous = set()
        combined = previous | disqualified
        with open(missing_path, "w") as handle:
            json.dump(sorted(combined), handle, indent=2)
        logger.info(
            "benched %d org(s) on board %r due to clearance/citizenship "
            "content trigger: %s",
            len(disqualified),
            board_name,
            sorted(disqualified),
        )

    save_failure_counts(failure_counts)
    save_seen(seen)
    save_last_run_state({
        "last_run": datetime.now(timezone.utc).isoformat(),
        "org_last_posted": org_last_posted,
    })
    # Thread the operator's seniority band through to filter_roles.
    # ``_PREFS_STATE["data"]`` is always populated by the singleton's
    # ``__init__`` so the ``.get(...)`` defaults are defensive rather
    # than load-bearing — a missing key would still produce None and
    # the band filter would no-op.
    prefs_data = _PREFS_STATE.get("data") or {}
    return filter_roles(
        results,
        min_seniority=prefs_data.get("min_seniority"),
        max_seniority=prefs_data.get("max_seniority"),
    )


def main():
    parser = argparse.ArgumentParser(description="Run all configured job-board scrapers")
    # When ``BOARDS_DELTA_HOURS`` is exported, its value flows through
    # to ``run_all``'s positional default at module-import time. We
    # mirror it here so a ``python -m runner`` invocation without an
    # explicit ``--delta-hours`` flag picks up the same env value a
    # direct ``run_all(...)`` call would — *only* when the operator
    # has set the env var. Unset env falls back to the legacy CLI
    # default (``1h``) so cron scripts that don't export
    # ``BOARDS_DELTA_HOURS`` keep their existing behavior; they have
    # not been "broken" by this change.
    delta_default = (
        DEFAULT_DELTA_HOURS
        if os.environ.get("BOARDS_DELTA_HOURS")
        else CLI_DELTA_HOURS_WHEN_ENV_UNSET  # preserved for cron scripts that don't export the env var
    )
    parser.add_argument("--delta-hours", type=int, default=delta_default)
    parser.add_argument("--boards", nargs="*", default=list(ORG_INDEX.keys()))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_all(delta_hours=args.delta_hours, boards=args.boards, limit=args.limit)


if __name__ == "__main__":
    main()
