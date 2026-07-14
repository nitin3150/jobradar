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
# Persistent, Postgres-backed dedupe store. Replaces the legacy
# on-disk ``backend/data/seen.json`` (ephemeral in GHA) so the
# ``seen_ids`` snapshot the runner hands to the fetchers
# survives between cron ticks. ``_postgres_backend_enabled``
# internally decides between the table and an on-disk fallback
# for local dev without Supabase env.
from services import board_seen
from services.profile_service import get_all_target_roles, load_profile
from utils.filters import (
    bench_org_from_text,
    filter_roles,
    is_relevant_role,
    min_years_required,
    should_reject_by_title,
)
from utils.http import build_client
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

# Slug → display name overrides for orgs where the title-case
# fallback would mangle the brand ("openai" → "Openai" looks broken;
# "xai" → "Xai" loses the lowercase x). Keep this list small — most
# slugs title-case cleanly. The keys are the ATS slugs as written in
# ``backend/data/{ashby,greenhouse,lever}_companies.json``.
_COMPANY_NAME_OVERRIDES: dict[str, str] = {
    "openai": "OpenAI",
    "xai": "xAI",
    "n8n": "n8n",
    "scale-ai": "Scale AI",
    "arize-ai": "Arize AI",
}


def _display_name_for_slug(slug: str) -> str:
    """Map an org slug (e.g. ``"replicate"``, ``"stripe-inc"``) to a
    human-readable company name for the JobCard UI.

    The override map handles brand names that title-case badly;
    the fallback is ``slug.replace("-", " ").title()`` which turns
    ``"stripe-inc"`` into ``"Stripe Inc"`` and ``"replicate"`` into
    ``"Replicate"``. The mapping is intentionally narrow — most
    slugs title-case cleanly and adding more entries drifts toward
    a maintenance burden the operator doesn't actually need.
    """
    if slug in _COMPANY_NAME_OVERRIDES:
        return _COMPANY_NAME_OVERRIDES[slug]
    return slug.replace("-", " ").title()

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


def _build_relevant_patterns_from_roles(
    target_roles: list[str],
) -> list[str]:
    """Convert a list of target role titles into case-insensitive regex
    patterns for :func:`utils.filters.is_relevant_role`.

    Each role becomes a strict substring match — ``"Senior AI Engineer"``
    in the profile produces a pattern that matches ``"Senior AI Engineer"``
    in a job title (and only that exact phrase). The LLM scorer handles
    the nuance of partial matches ("AI Engineer" vs "Senior AI Engineer")
    via the profile-aware SYSTEM_PROMPT; the regex filter is the coarse
    prefilter that just decides "is this role family one I care about?".

    Word-boundary semantics
    -----------------------

    Naive ``\\b`` boundaries fail on roles that start or end with a
    non-word character — ``"C++ Engineer"`` would produce
    ``"\\bC\\+\\+ Engineer\\b"`` and ``\\b`` doesn't fire between ``+``
    and a space (both are non-word characters), so the pattern misses
    titles like ``"Senior C++ Engineer"``. We use an explicit
    start/end alternation ``(?:^|(?<=\\s)) ... (?:$|(?=\\s))`` instead
    — it matches the role at the start of the string OR after a
    space, and at the end of the string OR before a space. This
    mimics a word boundary for plain text but correctly handles
    special characters at the edges.

    Earlier drafts used ``(?<!\\w) ... (?!\\w)`` (negative
    lookarounds), which failed at position 0 of a string because
    Python's regex engine can't evaluate a lookbehind that points
    before the start of the input — the assertion never satisfied
    and ``"AI Engineer"`` at the start of a title (e.g.
    ``"AI Engineer at Acme"``) silently didn't match. The
    start-anchored alternation sidesteps the issue.

    ``re.escape`` handles the role body (``"C++"`` → ``"C\\+\\+"``,
    ``"AI/ML"`` → ``"AI/ML"``) so a role with regex metacharacters
    doesn't accidentally become a regex of its own. Spaces are
    un-escaped after ``re.escape`` (Python 3.7+ escapes every
    non-alphanumeric, including the space between words) so the
    pattern string stays readable.

    Returns
    -------
    A list of compiled-ready regex pattern strings, one per role.
    An empty list when ``target_roles`` is empty (e.g. an operator
    who cleared their profile) — :func:`is_relevant_role` treats an
    empty ``extra_relevant_patterns`` as a no-op and falls back to
    ``DEFAULT_RELEVANT_PATTERNS``.
    """
    patterns: list[str] = []
    for role in target_roles:
        # Strip whitespace defensively — the profile renderer
        # already trims, but a hand-edited profile.yml could
        # sneak a leading/trailing space past the loader.
        cleaned = (role or "").strip()
        if not cleaned:
            continue
        # ``re.escape`` (Python 3.7+) escapes every non-alphanumeric
        # character, including the space between words. The escaped
        # form ``\ `` still matches a literal space in regex, but it
        # makes the pattern string harder to read and breaks tests
        # that substring-check the pattern. Un-escape spaces so the
        # output is ``"AI Engineer"`` rather than ``"AI\ Engineer"``.
        escaped = re.escape(cleaned).replace("\\ ", " ")
        # ``(?i)`` makes the pattern case-insensitive so a profile
        # role like ``"AI Engineer"`` matches ``"ai engineer"`` in
        # a job title. Job titles arrive in arbitrary case from
        # the ATS fetchers; the lowercased-title contract in
        # :func:`utils.filters.is_relevant_role` doesn't help here
        # because the pattern itself is case-sensitive by default
        # in Python's ``re`` module — we need the flag on the
        # pattern string, not on the search call.
        patterns.append(
            fr"(?i)(?:^|(?<=\s)){escaped}(?:$|(?=\s))"
        )
    return patterns


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

    excluded: set[str] = set()
    missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
    if missing_path.exists():
        with open(missing_path, "r") as handle:
            excluded.update(json.load(handle))
    # ``BOARDS_SKIP_TIMEOUTS=1`` excludes orgs in
    # ``<board>_timeout_orgs.json`` — the hourly active tier sets
    # this so the slow-org list (timed-out orgs the dormant tier
    # didn't recover) doesn't burn the hourly budget. The dormant
    # tier sets it to 0 (the default) so the daily run re-attempts
    # those orgs with a longer per-request timeout window
    # (BOARDS_HTTP_TIMEOUT=30), removing them from the slow list on
    # a successful fetch. Strict whitelist of truthy strings
    # (``"1"``/``"true"``/``"yes"``) so an accidental
    # ``BOARDS_SKIP_TIMEOUTS=2024`` (or any other random string)
    # doesn't accidentally enable skip mode and silently shrink
    # coverage. Comparison is lowercased to match YAML/GHA env-var
    # boolean conventions.
    _skip_value = os.environ.get("BOARDS_SKIP_TIMEOUTS", "0").strip().lower()
    if _skip_value in ("1", "true", "yes"):
        timeout_path = DATA_DIR / f"{board_name}_timeout_orgs.json"
        if timeout_path.exists():
            with open(timeout_path, "r") as handle:
                try:
                    excluded.update(json.load(handle))
                except (json.JSONDecodeError, TypeError):
                    pass
    # ``BOARDS_CADENCES="a,b,c"`` narrows the org list to whatever
    # lives under
    # ``data/enriched/<board>/cadence/<a|b|c>/`` — the per-tier
    # GHA workflow writes that env on the per-cron schedule
    # (see ``.github/workflows/boards-scan.yml``: active hourly,
    # dormant daily, probe weekly Sun). The per-cadence layout
    # produced by :mod:`scripts.enrich_org_profiles` means the
    # runner reads slugs as filenames (``Path.stem``), no JSON
    # parse, so the per-tick overhead stays under ~1 ms even at
    # the 10K-org scale.
    #
    # Missing cadence directories log a warning and contribute
    # zero slugs; an all-empty ``BOARDS_CADENCES`` resolution
    # logs a second warning so an operator chasing an empty
    # queue understands "tier ran cleanly but has no orgs" rather
    # than a phantom upstream failure. Both warnings are best-
    # effort; ``load_orgs`` returns the (possibly empty) filtered
    # list either way.
    #
    # The flat ``_skip_list.json`` reader that the deprecated
    # ``BOARDS_USE_ENRICHED_PROFILES`` env previously opened was
    # REMOVED — every org profile now lives in either
    # ``cadence/<bucket>/`` (scanned) or ``skip/`` (never
    # scanned) or ``errors/`` (operator inspection only). The
    # ``skip/`` directory does not need an explicit exclusion:
    # no GHA tier names it in ``BOARDS_CADENCES`` so it never
    # shows up in an active scan.
    #
    # Unset ``BOARDS_CADENCES`` = legacy behavior (full board
    # minus the missing/timeout ban lists above) so an
    # unconfigured deployment keeps scanning every org.
    _cadence_env = os.environ.get("BOARDS_CADENCES", "").strip()
    if _cadence_env:
        allowed_cadences = [
            c.strip() for c in _cadence_env.split(",") if c.strip()
        ]
        enriched_root = DATA_DIR / "enriched" / board_name
        allowed_slugs: set[str] = set()
        for cadence in allowed_cadences:
            cadence_dir = enriched_root / "cadence" / cadence
            if not cadence_dir.exists():
                logger.warning(
                    "BOARDS_CADENCES named cadence %r for board %r "
                    "but %s does not exist on disk; treating as zero-match",
                    cadence, board_name, cadence_dir,
                )
                continue
            for path in cadence_dir.glob("*.json"):
                allowed_slugs.add(path.stem)
        if not allowed_slugs:
            # Surface a loud warning so an operator staring at
            # an empty queue understands "this scan ran
            # successfully but the cadence tier has nothing to
            # fetch" rather than chasing a phantom upstream
            # failure. ``run_all`` will log "no relevant jobs"
            # and tidy-exit without writing a ``scanner_runs``
            # ``state=error`` row.
            logger.warning(
                "BOARDS_CADENCES=%r resolved to zero slugs for board %r; "
                "the scan will run end-to-end and produce zero writes",
                _cadence_env, board_name,
            )
        # ``orgs`` is intersected with the allowed set BEFORE the
        # ``if excluded:`` branch below so the missing-org /
        # timeout-org ban lists still apply on top of the cadence
        # filter (a slug that's both in the rare bucket AND has
        # been timing out for a week gets dropped either way).
        orgs = [slug for slug in orgs if slug in allowed_slugs]
    if excluded:
        return [slug for slug in orgs if slug not in excluded]
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
        # Distinct ``outcome="timeout"`` so ``run_all`` can promote
        # the slug to ``<board>_timeout_orgs.json`` after repeated
        # timeouts WITHOUT triggering the failure-threshold bench.
        # The hourly active tier sets ``BOARDS_SKIP_TIMEOUTS=1`` so
        # these orgs are excluded from the next run; the daily
        # dormant tier sets ``BOARDS_SKIP_TIMEOUTS=0`` and
        # ``BOARDS_HTTP_TIMEOUT=30`` so it re-attempts them with
        # room to breathe, removing them on success.
        print(f"Request timed out while scraping {board_name}/{slug}")
        return {"board": board_name, "slug": slug, "outcome": "timeout", "jobs": [], "new_ids": {}, "latest": None}
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
    # Persistent-seen: load per-board. The Postgres-backed
    # ``load_seen_for_board`` returns composite ``"<board>:<url>"`` keys
    # matching the formula :func:`services.scoring_service._job_id`
    # uses to derive the ``jobs.id`` UUID5 PK. The fetcher's own
    # ``seen_ids`` filter is a no-op against these keys (the fetcher
    # filters on raw ATS IDs) — the real pre-LLM dedupe is enforced
    # in the result-merge loop below, where we add a composite-key
    # check BEFORE passing the job to the scorer. Loading here on a
    # per-board basis keeps each BoardSeenJob query O(1) (a single
    # ``idx_board_seen_board_last_seen`` lookup) rather than a full
    # table scan per ``run_all`` invocation.
    # NOTE: the legacy ``seen`` dict is removed entirely — there is no
    # backwards-compat layer for raw ATS ID keys; a fresh deploy
    # pays a one-time re-walk cost on the first cron tick.
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
    # Per-board dedupe state. ``seen_ids_by_board`` is the
    # read-fetch snapshot (frozenset, consumed by the threading
    # loop); ``newly_marked_by_board`` accumulates the dedupe
    # tuples we hand to :func:`board_seen.record_seen_batch` at
    # the end.
    seen_ids_by_board: dict[str, frozenset[str]] = {}
    newly_marked_by_board: dict[str, list[tuple[str, str]]] = {
        board: [] for board in boards
    }
    # Slow-org tracking: each (board, slug) that timed out on this run
    # lands here. ``run_all`` writes ``<board>_timeout_orgs.json`` after
    # the fetch loop so the next run (with ``BOARDS_SKIP_TIMEOUTS=1``
    # on the hourly tier) skips it. The daily tier runs without that
    # env var set, so it re-attempts the slow orgs with a longer
    # ``BOARDS_HTTP_TIMEOUT`` and unfreezes them on success.
    newly_timeout = {board: set() for board in boards}
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
                # Snapshot the *already-seen* composite keys for this
                # board ONCE before submitting futures so the
                # per-thread pool only sees an immutable frozenset.
                # The keys are ``"<board>:<url>"`` (see
                # :func:`services.board_seen.dedupe_key`); the
                # fetcher's ``seen_ids`` parameter accepts raw ATS
                # IDs, so this snapshot will look mostly empty to the
                # fetcher's check — that's intentional, the real
                # pre-LLM dedupe happens in the result-merge loop
                # below.
                seen_ids_by_board[board_name] = (
                    board_seen.load_seen_for_board(board_name)
                )
                seen_ids_for_fetch = seen_ids_by_board[board_name]
                for slug in orgs:
                    futures.append(
                        executor.submit(execute_fetch, fetcher, board_name, slug, since, seen_ids_for_fetch, client)
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
                        # Pre-LLM composite-key dedupe. The fetcher's
                        # own ``seen_ids`` filter checks raw ATS IDs
                        # which don't match our persistent
                        # ``"<board>:<url>"`` keys, so we MUST add
                        # this check here BEFORE any LLM scoring cost
                        # is incurred. Otherwise every cron tick would
                        # re-score every job the runner has ever seen
                        # (the operator reported LLM cost symptom on
                        # the GHA backfill tick).
                        job_url = job.get("url") or ""
                        composite_key = board_seen.dedupe_key(
                            board_name, job_url
                        )
                        seen_set = seen_ids_by_board.get(board_name)
                        if seen_set is not None and composite_key in seen_set:
                            logger.debug(
                                "skip %s/%s: already-seen composite key %s",
                                board_name, slug, composite_key,
                            )
                            continue
                        title = job.get("title") or ""
                        description = (
                            job.get("description") or job.get("content") or ""
                        )
                        # Title-level reject: the operator's experience
                        # does not align with staff/principal/lead/head/
                        # director roles regardless of the band filter
                        # outcome. should_reject_by_title reads
                        # BOARDS_REJECT_TITLE_KEYWORDS for env-driven
                        # narrowing; the default 5-token set matches
                        # the per-profile reject list. The debug log
                        # surfaces a *truncated* title (some ATS
                        # payloads interpolate PII into the title
                        # field -- e.g. hiring-manager names -- and
                        # GHA logs are world-readable to anyone with
                        # the repo on Actions). Runs BEFORE the
                        # years-floor gate because the title-reject
                        # regex is cheaper than the longer combined
                        # title+description scan in
                        # :func:`utils.filters.min_years_required`:
                        # skipping that scan on already-rejected
                        # jobs saves wall-clock on the hot loop.
                        if should_reject_by_title(title):
                            # Truncate to 80 chars to bound log
                            # output and reduce PII surface.
                            logger.debug(
                                "drop %s/%s: title reject (seniority keyword in %r)",
                                board_name, slug, title[:80],
                            )
                            continue
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
                        # Tag the job with a human-readable company name so
                        # downstream consumers (scoring_service, boards_scan)
                        # can persist a real value instead of the literal
                        # "(unknown)" fallback. The ATS fetchers don't surface
                        # the company on each posting; the org slug (e.g.
                        # "replicate", "stripe-inc") is the only identifier
                        # the runner has. The override map handles the brand
                        # names title-casing mangles ("openai" → "Openai"
                        # without it), the title-case fallback handles
                        # hyphenated slugs ("stripe-inc" → "Stripe Inc"), and
                        # the ``not in`` guard keeps a future fetcher that
                        # returns a real company_name from being clobbered.
                        if "company_name" not in job or not job["company_name"]:
                            job["company_name"] = _display_name_for_slug(slug)
                        # Inject the SPECIFIC board ("ashby" | "greenhouse"
                        # | "lever") on every job — the boards_scan.py
                        # GHA persist path used to write a hardcoded
                        # "boards" string, which the React JobCard
                        # title-cased to "Board" (the operator's reported
                        # "Board name is saying Board it should say the
                        # Boards name like lever, ashby or greenhouse").
                        # Setting it here keeps the value consistent
                        # across both persist paths (Supabase REST via
                        # boards_scan + SQLAlchemy via scoring_service).
                        # The ``not in`` guard preserves any future fetcher
                        # that already populates ``ats_type``.
                        if "ats_type" not in job or not job.get("ats_type"):
                            job["ats_type"] = board_name
                        kept_after_gates.append(job)
                    results.extend(kept_after_gates)
                    # Record the composite keys of jobs that
                    # SURVIVED the gate filter — these are the
                    # dedupe keys we'll persist to ``board_seen_jobs``
                    # at the end of the run. We deliberately only
                    # record keys for jobs that made it past
                    # ``kept_after_gates``; bench/timeout/error
                    # outcomes don't generate new dedupe keys
                    # because the next run's per-org fetch will
                    # re-emit them via the since-filter anyway.
                    for job in kept_after_gates:
                        job_url = job.get("url") or ""
                        if not job_url:
                            continue
                        composite_key = board_seen.dedupe_key(
                            board_name, job_url
                        )
                        # Latest observation timestamp — prefer the
                        # parsed posted_at (the immutable first-post
                        # date), fall back to the raw ``new_ids``
                        # stamp the fetcher produced.
                        posted_dt = job.get("posted_at")
                        stamp_iso = (
                            posted_dt.astimezone(timezone.utc).isoformat()
                            if isinstance(posted_dt, datetime)
                            else None
                        )
                        if stamp_iso is None:
                            # Use the first raw-ATS-id stamp the
                            # fetcher gave us as the secondary
                            # timestamp source. ``r["new_ids"]`` is
                            # keyed on raw ATS IDs; we have no
                            # obvious lookup, so fall back to the
                            # largest stamp in the dict (newest
                            # observed).
                            stamps = [
                                s for s in r["new_ids"].values() if s
                            ]
                            if stamps:
                                stamp_iso = max(stamps)
                        if stamp_iso:
                            newly_marked_by_board[board_name].append(
                                (composite_key, stamp_iso)
                            )
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
                elif outcome == "timeout":
                    # Track the slug on the per-board timeout list so
                    # the next hourly run (BOARDS_SKIP_TIMEOUTS=1) skips
                    # it. The daily dormant tier re-attempts without the
                    # skip flag and with BOARDS_HTTP_TIMEOUT=30 to give
                    # the slow orgs room to breathe. Successful fetch
                    # removes the slug from the file (see writeback
                    # below).
                    newly_timeout[board_name].add(slug)
                # "error" -> transient; leave failure count untouched, don't bench
    finally:
        client.close()

    _write_missing_lists(boards, newly_missing, recovered)
    # Slow-org writeback: merge freshly-timed-out slugs into
    # ``<board>_timeout_orgs.json`` and drop any that came back to
    # life on this run (the ``recovered`` set carries all
    # ``outcome="ok"`` slugs). The hourly active tier sets
    # ``BOARDS_SKIP_TIMEOUTS=1`` so ``load_orgs`` filters these out
    # on the next tick; the daily dormant tier leaves the flag at 0
    # so it re-attempts them with BOARDS_HTTP_TIMEOUT=30. A slug that
    # succeeds on the daily tier gets removed from the file here.
    for board_name in boards:
        timeout_path = DATA_DIR / f"{board_name}_timeout_orgs.json"
        previous_timeouts: set[str] = set()
        if timeout_path.exists():
            with open(timeout_path, "r") as handle:
                try:
                    previous_timeouts = set(json.load(handle))
                except (json.JSONDecodeError, TypeError):
                    previous_timeouts = set()
        combined_timeouts = (
            previous_timeouts
            | newly_timeout.get(board_name, set())
        ) - recovered.get(board_name, set())
        if combined_timeouts:
            with open(timeout_path, "w") as handle:
                json.dump(sorted(combined_timeouts), handle, indent=2)
            logger.info(
                "slow-org list for board %r: %d org(s) skipped on next hourly run: %s",
                board_name,
                len(combined_timeouts),
                sorted(combined_timeouts),
            )
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
    # Persist the dedupe state via
    # :func:`services.board_seen.record_seen_batch`. One call per
    # board so errors are localised and the operator log lines
    # surface the failing board name. We swallow write failures
    # here so the boards-scan cron doesn't crash the runner just
    # because the dedupe-store INSERT slipped — the LLM scoring
    # already gave the operator the value they wanted via the
    # ``results`` list; losing one dedupe iteration is recoverable
    # via next-tick re-walk.
    for board_name in boards:
        items = newly_marked_by_board.get(board_name) or []
        if not items:
            continue
        try:
            written = board_seen.record_seen_batch(board_name, items)
            logger.info(
                "board_seen: recorded %d new dedupe keys for board %r",
                written, board_name,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort persist
            logger.warning(
                "board_seen: failed to record dedupe keys for board %r (%s); "
                "next tick will re-walk this batch.",
                board_name, type(exc).__name__,
            )
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
    # Load the operator's profile ONCE per scan and pass the target
    # roles as additional positive-relevance patterns. The
    # ``profile_service`` module-level cache makes the second access
    # free; loading here (rather than inside :func:`filter_roles`)
    # keeps the per-job loop allocation-free. The empty-profile case
    # (operator cleared their YAML or only the example file is
    # present) yields an empty list — the filter falls back to
    # ``DEFAULT_RELEVANT_PATTERNS`` so the legacy keyword behaviour
    # is preserved bit-for-bit.
    profile = load_profile()
    target_roles = get_all_target_roles(profile)
    extra_relevant_patterns = _build_relevant_patterns_from_roles(target_roles)
    return filter_roles(
        results,
        min_seniority=prefs_data.get("min_seniority"),
        max_seniority=prefs_data.get("max_seniority"),
        extra_relevant_patterns=extra_relevant_patterns,
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
