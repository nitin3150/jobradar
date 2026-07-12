"""One-time LLM enrichment for orgs across the 3 ATS boards.

Why this exists
===============

The boards scanner runs hourly on GHA and burns LLM tokens on every
fresh opportunity, even opportunities at orgs that are obviously
non-tech, sponsorship-closed, or haven't posted in 6+ months. This
script runs **once** to classify every org into a structured
``OrgProfile`` JSON, then rebuilds a per-board ``_skip_list.json``
that the boards runner consults to drop those orgs from the hourly
cron. Output lands in:

    data/enriched/<board>/<slug>.json     # one OrgProfile per org
    data/enriched/<board>/_skip_list.json # slugs the runner should skip

The expensive LLM call (one per org) happens once. The cheap
per-board file read happens on every cron. The boards runner only
consults the skip list when ``BOARDS_USE_ENRICHED_PROFILES=1`` —
default 0 preserves the existing behavior bit-for-bit.

Re-run manually when an operator wants to refresh the
classification — the script is idempotent (``--skip-existing`` is
the default; ``--force`` rewrites ``status:ok`` rows):

    python scripts/enrich_org_profiles.py --board all
    python scripts/enrich_org_profiles.py --board greenhouse --slugs stripe

Cost: ~10K orgs across the 3 boards * ~$0.003/call = ~$30-50
one-time spend at the default NVIDIA RPM. Reads the same LLMClient
chain (``NVIDIA_API_KEY`` -> ``NVIDIA_API_KEY_2`` -> ``GROQ_API_KEY``)
the rest of the project uses, so no new credentials are needed.

Failure handling
================

LLM call failures or Pydantic-validation failures write
``{status: failed, error: "..."}`` files. The skip-list writer
ignores any profile whose ``status != "ok"``, so a partial run
leaving 50% orgs as ``status: failed`` does not pollute the
skip list — those orgs are still fetched by current behavior.

Schema versioning
=================

``SCHEMA_VERSION`` bumps whenever the wire shape of
``data/enriched/<board>/<slug>.json`` changes so a mixed-version
run is acceptable during a rolling refresh. The skip-list writer
also stamps ``schema_version`` so a future runner can refuse to
consume a list from an older schema (cross-major refresh).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ``scripts/X.py`` boot path — make ``backend/`` importable so
# ``from pipeline...`` / ``from services...`` resolves, matching the
# pattern in ``scripts/boards_scan.py``.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import BaseModel, Field, ValidationError

from pipeline.nodes.jobs_boards.runner import ORG_INDEX
from services.llm_client import LLMClient, parse_profile_response
from utils.http import build_client

# Output layout
DATA_DIR = BACKEND_ROOT / "data"
PROFILE_DIR = DATA_DIR / "enriched"

# Schema version — bump when wire shape changes. Bumping this also
# changes the runner's contract (it should refuse to consume lists
# from mismatched versions). v0 (no version) profiles are treated as
# failed/missing by the skip-list writer, forcing a re-enrich.
SCHEMA_VERSION = 1

# Skip rules — kept inline (no env override) so the behavior is
# one-line interpretable. Tunable later if the operator measures
# false-positive or false-negative skip rates.
MIN_JOBS_FOR_LLM = 3
SKIP_CADENCE_DEAD = frozenset({"dead", "rare"})
SKIP_CADENCE_STALE_DAYS = 180
SKIP_TECH_RATIO_THRESHOLD = 0.15
SKIP_CONFIDENCE_THRESHOLD = 0.7

# Concurrency — mirrors boards_runner.MAX_WORKERS for fetches and
# boards_scan._score_all for the LLM signature.
MAX_FETCH_WORKERS = 8
LLM_CONCURRENCY = 8

# LLM input caps. Boards fetcher returns up to a few hundred jobs
# per org; we cap to keep the prompt under ~2.5K input tokens so
# NVIDIA/Groq are both cheap.
MAX_JOBS_TO_LLM = 30
MAX_DESCRIPTION_CHARS = 600

logger = logging.getLogger("jobradar.enrich")


# ---------------------------------------------------------------------------
# On-disk schema
# ---------------------------------------------------------------------------
class OrgProfile(BaseModel):
    """Per-org LLM classification. Single source of truth for the
    shape written to ``data/enriched/<board>/<slug>.json`` AND for the
    fields the runner reads from ``_skip_list.json``.

    Pydantic enforces field types and ranges on write so a malformed
    LLM response lands in a ``{"status": "failed", "error": ...}``
    envelope instead of corrupting the JSON contract.
    """

    schema_version: int = SCHEMA_VERSION
    slug: str
    board: str
    enriched_at: str
    source_jobs_count: int
    source_last_published: str | None = None

    # Strict categorical — the runner branches on these directly.
    primary_function: str
    estimated_stage: str
    hiring_volume_estimate: str
    posting_cadence: str

    # Hard claims — only set when postings EXPLICITLY mention the
    # topic. ``None`` means "no info"; ``False`` means an explicit
    # "no sponsorship" mention surfaces a positive skip signal.
    sponsorship_open: bool | None = None
    clearance_required: bool | None = None
    remote_friendly: bool | None = None
    is_likely_startup: bool | None = None

    # Soft signals — chance floats. Runner treats ``>=0.6`` as yes,
    # ``<0.4`` as no, ``0.4-0.6`` as borderline.
    tech_role_ratio: float = Field(ge=0.0, le=1.0)
    sponsorship_likelihood: float = Field(ge=0.0, le=1.0, default=0.5)
    clearance_likelihood: float = Field(ge=0.0, le=1.0, default=0.0)
    startup_likelihood: float = Field(ge=0.0, le=1.0, default=0.5)
    volatility_signal: float = Field(ge=0.0, le=1.0, default=0.0)

    # Free-form + meta.
    notes: str = ""
    overall_confidence: float = Field(ge=0.0, le=1.0)
    model_used: str = ""


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------
ENRICHMENT_SYSTEM_PROMPT = (
    "You are a job-board analyst classifier. Given the full public job "
    "board for a single organisation (Greenhouse, Lever, or Ashby), "
    "classify the org along these axes and return strict JSON with this "
    "exact shape:\n\n"
    "{\n"
    '  "primary_function": <one of: engineering_heavy | product_heavy | '
    'sales_heavy | ops_heavy | mixed | non_tech_satellite | hiring_paused>,\n'
    '  "tech_role_ratio": <float 0.0-1.0 — fraction of postings that are '
    "core engineering / product / AI / data roles>,\n"
    '  "sponsorship_open": <true | false | null — only set to true/false '
    "if postings EXPLICITLY mention visa sponsorship; null if no "
    "mention>,\n"
    '  "sponsorship_likelihood": <float 0.0-1.0 — soft estimate; 1.0 = '
    "explicit open, 0.0 = explicit blocked, 0.5 = no info. When "
    "sponsorship_open is null AND postings don't MENTION sponsorship, "
    "set this <= 0.4 to bias toward 'unknown, don't trust' rather than "
    "default neutral 0.5 — we treat 0.4-0.6 as borderline>,\n"
    '  "clearance_required": <true | false | null>,\n'
    '  "clearance_likelihood": <float 0.0-1.0>,\n'
    '  "remote_friendly": <true | false | null — true when >50% of '
    'postings are remote or hybrid>,\n'
    '  "is_likely_startup": <true | false | null>,\n'
    '  "startup_likelihood": <float 0.0-1.0>,\n'
    '  "estimated_stage": <one of: idea | pre_seed | seed | series_a | '
    'series_b | series_c_plus | public | unknown>,\n'
    '  "hiring_volume_estimate": <one of: lt_10 | 10_50 | 50_200 | 200_1000 '
    '| gt_1000 | unknown>,\n'
    '  "posting_cadence": <one of: daily | few_per_week | weekly | '
    'biweekly | monthly | quarterly | rare | dead | unknown>,\n'
    '  "volatility_signal": <float 0.0-1.0 — 0.0 stable, 1.0 lots of '
    'duplicate IDs / rapid reposting>,\n'
    '  "notes": <string, <=200 chars>,\n'
    '  "overall_confidence": <float 0.0-1.0 — your self-rated confidence '
    'in the answers above, including how much signal the input has>\n'
    "}\n\n"
    "Rules:\n"
    "- tech_role_ratio is the explicit fraction of tech-family postings. "
    "Default-to-tech roles: Software / Backend / Frontend / Mobile Engineer, "
    "ML/AI Engineer/Researcher, Data Engineer/Scientist, Platform, DevOps/SRE, "
    "Security, Solutions Engineer, UX Engineer. Extend with judgment for "
    "engineering-shaped adjacent titles (Tech Lead, Architect, QA/Test "
    "Engineer) when the role's day-to-day is engineering. Designer / "
    "Product Designer roles count when the org's product surface is "
    "design-oriented (design tools, IDEs); do NOT inflate tech_role_ratio "
    "with marketing or brand-designer roles at non-design orgs.\n"
    "- Anti-GTM bias: Tech startups scaling revenue flood boards with GTM "
    "roles (Account Executive, Customer Success, Sales, SDR). Do NOT flag "
    "as `sales_heavy` if the org still has a strong engineering core. Count "
    "Solutions Engineers as tech, but look beyond pure GTM posting volume.\n"
    "- Self-consistency: If `primary_function` is `engineering_heavy` or "
    "`product_heavy`, you MUST compute a `tech_role_ratio` >= 0.5. If these "
    "conflict, re-evaluate and recount the roles before outputting.\n"
    "- Use null for sponsorship/clearance when postings don't MENTION "
    "the topic. False positives on `sponsorship_open = true` cause "
    "skipping real sponsor-blocked orgs; weight explicit-only.\n"
    "- primary_function is ONE bucket — pick the single dominant one. "
    'Use "mixed" when no single bucket dominates. Use '
    '"non_tech_satellite" for non-tech orgs that post a few engineering '
    "roles (e.g. finance firms with an internal IT team).\n"
    "- posting_cadence reflects how often the org posts NEW jobs. "
    "'dead' = no postings in 6+ months.\n"
    "- overall_confidence reflects certainty (0.3=uncertain, 0.9=high). "
    "If you categorize it as `engineering_heavy` but you compute a "
    "`tech_role_ratio` < 0.5, cap this at 0.5 to flag uncertainty.\n"
    "- Return ONLY the JSON object. No markdown, no preamble, no "
    "code fences."
)


# ---------------------------------------------------------------------------
# Trimming + prompt builders
# ---------------------------------------------------------------------------
def _trim_jobs_for_prompt(jobs: list[dict]) -> list[dict]:
    """Compress a board-fetch job list to classification-bearing fields.

    The fetcher returns rich dicts (id, title, url, published_at,
    description, ...). For org classification we only need the
    fields the LLM actually reasons over. Trim before the LLM call
    so the prompt stays well under the ~3K input-token budget.
    """
    trimmed: list[dict] = []
    for j in jobs[:MAX_JOBS_TO_LLM]:
        title = j.get("title") or ""
        description = (j.get("description") or j.get("content") or "")[:MAX_DESCRIPTION_CHARS]
        location = j.get("location") or ""
        published = j.get("published_at") or ""
        if hasattr(published, "isoformat"):
            published = published.isoformat()
        trimmed.append(
            {
                "title": title,
                "description": description,
                "location": location,
                "first_published": published,
            }
        )
    return trimmed


def _build_user_prompt(board: str, slug: str, jobs: list[dict]) -> str:
    """Render the user-side prompt: org identity + trimmed jobs.

    The boards fetchers don't surface a display company name on each
    posting — the runner derives it from the slug at insert time.
    We pass the slug as the only identity hint and ask the LLM to
    focus on roles.
    """
    org_label = f"{board}:{slug}"
    jobs_for_prompt = _trim_jobs_for_prompt(jobs)
    job_dump = json.dumps(jobs_for_prompt, indent=2, default=str)
    return (
        f"Org board: {org_label}\n"
        f"Total jobs in board response: {len(jobs)} "
        f"(trimmed to first {len(jobs_for_prompt)} for the LLM prompt)\n\n"
        f"Jobs (JSON; title, description, location, first_published):\n"
        f"{job_dump}\n\n"
        "Return the strict JSON profile object — no markdown, no preamble."
    )


def _latest_iso_published(jobs: list[dict]) -> str | None:
    """Return the latest ISO 8601 ``published_at`` across the org's
    jobs, or ``None`` if no parseable timestamp is found.

    Used to populate ``source_last_published`` so the skip rule
    ``cadence=dead AND last_posted > 180d ago`` can run on the raw
    signal independent of the LLM's ``posting_cadence`` derivation.
    """
    latest: str | None = None
    for j in jobs:
        ts = j.get("published_at")
        if ts is None:
            continue
        if hasattr(ts, "isoformat"):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts)
        if latest is None or ts_str > latest:
            latest = ts_str
    return latest


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, payload: dict, *, indent: int = 2) -> None:
    """Write ``payload`` to ``path`` atomically.

    Pattern: dump to ``<path>.tmp`` in the same directory, ``fsync``,
    ``os.replace``. ``os.replace`` is atomic on POSIX within the same
    filesystem, so a concurrent reader (the boards runner's
    ``load_orgs()``) never sees a half-written file. ``shutil.move`` is
    NOT atomic across filesystems — we use ``os.replace`` to keep
    the contract.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(payload, tmp, indent=indent, default=str, sort_keys=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _write_status_envelope(
    *,
    board: str,
    slug: str,
    status: str,
    reason_or_error: str,
    extra: dict | None = None,
) -> Path:
    """Write a non-OK envelope (``status: skipped`` or
    ``status: failed``). These envelopes are explicitly ignored by the
    skip-list writer; the runner reads them as "no profile
    available, fall through to current behavior".
    """
    payload: dict = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "slug": slug,
        "board": board,
        "decision_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason_or_error,
    }
    if extra:
        payload.update(extra)
    out = PROFILE_DIR / board / f"{slug}.json"
    _atomic_write_json(out, payload)
    return out


# ---------------------------------------------------------------------------
# Single-org enrichment
# ---------------------------------------------------------------------------
async def _enrich_one_org(
    llm: LLMClient,
    board: str,
    slug: str,
    jobs: list[dict],
    *,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str, Path, dict]:
    """Run ONE LLM call for ONE org and persist the result.

    Returns ``(slug, status, path, payload)`` where ``status`` is one
    of ``"ok"``, ``"skipped"``, ``"failed"``. ``path`` always points
    at the on-disk file so the caller can log it.

    Errors are caught and translated to a ``status: failed``
    envelope — the run never crashes mid-loop because one org's LLM
    call returned malformed JSON.

    Concurrency contract
    ---------------------

    This is a single ``async def`` coroutine, called from one outer
    ``asyncio.run(...)`` block in :func:`main`. The ``semaphore``
    parameter is created in the same outer loop and ``async with``
    works against it normally. Do NOT wrap this in
    ``asyncio.to_thread`` — the inner ``asyncio.run`` would create a
    second loop and ``asyncio.Semaphore`` is bound to the loop where
    it was created (the cross-loop access raises
    ``RuntimeError``). The LLMClient's internal ``AsyncTokenBucket``
    has the same constraint — multiple loops fighting for one
    ``asyncio.Lock`` break. Single-loop async is the design.
    """
    sys_prompt = ENRICHMENT_SYSTEM_PROMPT
    user_prompt = _build_user_prompt(board, slug, jobs)
    latest = _latest_iso_published(jobs)

    try:
        async with semaphore:
            content, model = await llm.run_json_prompt(
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                max_tokens=600,
                temperature=0.0,
            )
        parsed = parse_profile_response(content)
        profile = OrgProfile(
            slug=slug,
            board=board,
            enriched_at=datetime.now(timezone.utc).isoformat(),
            source_jobs_count=len(jobs),
            source_last_published=latest,
            model_used=model,
            **parsed,
        )
        out_path = PROFILE_DIR / board / f"{slug}.json"
        # Stamp ``status: "ok"`` on the persisted payload so the
        # ``main()`` skip-existing check (``existing_profile.get("status") == "ok"``)
        # and the ``_compute_skip_for_profile`` short-circuit both
        # recognise successful profiles. Without this field, every
        # re-run silently re-LLMs already-classified orgs and the
        # ``_skip_list.json`` mechanism stays empty regardless of
        # content because the ``status != "ok"`` guard short-circuits
        # to False on every real profile.
        payload = profile.model_dump()
        payload["status"] = "ok"
        _atomic_write_json(out_path, payload)
        return slug, "ok", out_path, payload
    except ValidationError as exc:
        path = _write_status_envelope(
            board=board,
            slug=slug,
            status="failed",
            reason_or_error=f"pydantic_validation: {exc.errors()[0].get('msg', '')}"[:200],
        )
        return slug, "failed", path, {}
    except Exception as exc:
        # ``_PermanentError`` from ``services.llm_client`` (parse
        # failure, all-providers-failed), HTTP timeout, anything
        # else — log+continue via the envelope, never crash the loop.
        path = _write_status_envelope(
            board=board,
            slug=slug,
            status="failed",
            reason_or_error=f"{type(exc).__name__}: {exc}"[:1000],
        )
        return slug, "failed", path, {}


def _fetch_one(fetcher, board: str, slug: str, client) -> tuple[str, list[dict]]:
    """Synchronous fetcher call wrapped for ThreadPoolExecutor.

    Returns ``(slug, jobs)``. Non-OK outcomes (HTTP 4xx, 5xx up to
    the retry budget, network blips) return ``(slug, [])`` so the
    orchestrator writes a ``status: failed`` envelope and
    continues. We deliberately do NOT ``raise`` here — a single
    org's flaky ATS response should not abort a 10K-org sweep.
    """
    try:
        result = fetcher(slug, client=client, since=None, seen_ids=frozenset())
        return slug, result.get("jobs") or []
    except Exception as exc:  # noqa: BLE001 — best-effort, log+continue
        logger.warning("fetch failed for %s/%s: %s", board, slug, exc)
        return slug, []


# ---------------------------------------------------------------------------
# Skip-list builder (Phase 3)
# ---------------------------------------------------------------------------
def _compute_skip_for_profile(profile: dict, *, now: datetime) -> bool:
    """Return True when the org should land on the per-board skip list.

    Rules — any one is sufficient:
    1. ``posting_cadence ∈ {dead, rare}`` AND
       ``source_last_published`` is older than
       ``SKIP_CADENCE_STALE_DAYS``. The raw timestamp (LLM-independent)
       protects against an over-eager LLM classifying a stale
       dead-board as "weekly".
    2. ``sponsorship_open is False`` — explicit sponsor-block.
       ``None`` (unknown) and ``True`` (open) do NOT trigger skip.
    3. ``tech_role_ratio < SKIP_TECH_RATIO_THRESHOLD`` AND
       ``overall_confidence > SKIP_CONFIDENCE_THRESHOLD`` —
       conjunction (NOT ``>=``) so a borderline LLM call still
       falls through to current regex scoring rather than
       silently dropping the org.

    Returns False unconditionally for profiles whose
    ``status != 'ok'`` — failed/skipped envelopes never leak into
    the skip list.
    """
    if not isinstance(profile, dict) or profile.get("status") != "ok":
        return False
    # Rule 1: cadence + raw-age
    cadence = profile.get("posting_cadence") or ""
    if cadence in SKIP_CADENCE_DEAD:
        last = profile.get("source_last_published")
        if last:
            try:
                when = datetime.fromisoformat(last)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if (now - when) > timedelta(days=SKIP_CADENCE_STALE_DAYS):
                    return True
            except (TypeError, ValueError):
                pass
    # Rule 2: explicit sponsor-block
    if profile.get("sponsorship_open") is False:
        return True
    # Rule 3: confidently non-tech
    tech_ratio = profile.get("tech_role_ratio")
    confidence = profile.get("overall_confidence")
    if tech_ratio is not None and confidence is not None:
        if (
            tech_ratio < SKIP_TECH_RATIO_THRESHOLD
            and confidence > SKIP_CONFIDENCE_THRESHOLD
        ):
            return True
    return False


def _build_skip_list(board: str, *, now: datetime | None = None) -> list[str]:
    """Walk ``data/enriched/<board>/*.json`` and return slug list.

    Sorted for determinism (handy for diff-stable PRs when an
    operator refreshes a single board). Filters out the meta
    ``_skip_list.json`` file and any failed/skipped envelopes.
    """
    profile_dir = PROFILE_DIR / board
    if not profile_dir.exists():
        return []
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    for path in sorted(profile_dir.glob("*.json")):
        # ``_skip_list.json`` is a meta file, never a profile.
        if path.name.startswith("_"):
            continue
        try:
            with open(path, "r") as f:
                profile = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("could not read profile %s (%s); skipping", path, exc)
            continue
        if _compute_skip_for_profile(profile, now=now):
            slug = profile.get("slug")
            if slug:
                out.append(slug)
    return out


def _write_skip_list(board: str, slugs: list[str]) -> None:
    """Atomic write of the per-board ``_skip_list.json``.

    Same atomic-write contract as ``_atomic_write_json`` — the
    runner's ``load_orgs()`` can read the file at any point and
    either see the previous version cleanly OR the new version
    cleanly, never a torn write.
    """
    out = PROFILE_DIR / board / "_skip_list.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "board": board,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "slugs": sorted(slugs),
    }
    _atomic_write_json(out, payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    p.add_argument(
        "--board",
        choices=["all", "ashby", "greenhouse", "lever"],
        default="all",
        help="Which board to enrich. Default: all.",
    )
    p.add_argument(
        "--slugs",
        default=None,
        help="Comma-separated slug subset to enrich (overrides --board if both set).",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=MAX_FETCH_WORKERS,
        help=f"Concurrent fetcher threads. Default {MAX_FETCH_WORKERS}.",
    )
    p.add_argument(
        "--min-jobs",
        type=int,
        default=MIN_JOBS_FOR_LLM,
        help=f"Below this job count the org is `status: skipped`. Default {MIN_JOBS_FOR_LLM}.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute skip list (Phase 3) but skip Phase 1 fetches + Phase 2 LLM calls.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-enrich even when an existing `status: ok` profile is on disk.",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Default: only enrich orgs without an existing `status: ok` profile.",
    )
    p.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Enrich every org regardless of existing profile.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="[enrich] %(asctime)s %(levelname)s %(message)s",
    )

    boards: list[str]
    if args.board == "all":
        boards = sorted(ORG_INDEX.keys())
    else:
        boards = [args.board]

    slugs_filter: set[str] | None = None
    if args.slugs:
        slugs_filter = {s.strip() for s in args.slugs.split(",") if s.strip()}

    # ============== Phase 1: fetch every (board, slug) ==============
    work_items: list[tuple[str, str, list[dict]]] = []
    org_count = 0
    skipped_short = 0
    skipped_existing = 0
    failed_fetch = 0

    client = build_client()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_meta: dict = {}
            for board in boards:
                orgs_path, fetcher = ORG_INDEX[board]
                with open(orgs_path, "r") as f:
                    orgs = json.load(f)
                for slug in orgs:
                    if slugs_filter and slug not in slugs_filter:
                        continue
                    org_count += 1
                    future_to_meta[
                        executor.submit(_fetch_one, fetcher, board, slug, client)
                    ] = (board, slug)

            for future in as_completed(future_to_meta):
                board, slug = future_to_meta[future]
                _slug, jobs = future.result()
                if not jobs:
                    # Empty fetch (transient or 404). Treat as failed ONLY
                    # if there's no prior profile — keep existing data on
                    # flaky upstreams rather than churning on-disk state.
                    existing = PROFILE_DIR / board / f"{slug}.json"
                    if not existing.exists():
                        _write_status_envelope(
                            board=board,
                            slug=slug,
                            status="failed",
                            reason_or_error="fetch returned empty or errored (transient)",
                        )
                        failed_fetch += 1
                    continue
                if len(jobs) < args.min_jobs:
                    _write_status_envelope(
                        board=board,
                        slug=slug,
                        status="skipped",
                        reason_or_error=f"fewer_than_{args.min_jobs}_jobs",
                        extra={"source_jobs_count": len(jobs)},
                    )
                    skipped_short += 1
                    continue
                existing = PROFILE_DIR / board / f"{slug}.json"
                if args.skip_existing and existing.exists() and not args.force:
                    try:
                        with open(existing, "r") as f:
                            existing_profile = json.load(f)
                        if existing_profile.get("status") == "ok":
                            skipped_existing += 1
                            continue
                    except (json.JSONDecodeError, OSError):
                        pass  # Treat unreadable existing as "needs rewrite"
                work_items.append((board, slug, jobs))
    finally:
        client.close()

    print(
        f"[enrich] fetch summary: orgs={org_count} work_items={len(work_items)} "
        f"skipped_short={skipped_short} skipped_existing={skipped_existing} "
        f"failed_fetch={failed_fetch}",
        flush=True,
    )

    if args.dry_run:
        # In dry-run we still rebuild the skip list from any existing
        # profiles on disk so the operator can preview what WOULD change.
        for board in boards:
            slugs_to_skip = _build_skip_list(board)
            print(
                f"[enrich] (dry-run) skip list for {board}: "
                f"{len(slugs_to_skip)} slugs",
                flush=True,
            )
        return 0

    # ============== Phase 2: enrich via LLM (bounded concurrency) =====
    succeeded = 0
    failed = 0
    if work_items:
        try:
            llm = LLMClient.from_env()
        except RuntimeError as exc:
            print(f"[enrich] ERROR LLM client init: {exc}", flush=True)
            return 1

        async def _drive() -> list[tuple[str, str, Path, dict]]:
            semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
            # All LLM coroutines share the same outer loop, so the
            # semaphore bounds in-flight calls cleanly without a
            # cross-loop ``asyncio.run`` shadow event. ``asyncio.gather``
            # schedules all ``LLM_CONCURRENCY`` tasks at once; the
            # semaphore gates the actual ``await llm.run_json_prompt``
            # honoring NVIDIA's ``AsyncTokenBucket`` RPM cap below.
            # Standard ``gather`` (no return_exceptions): the inner
            # ``except Exception`` blocks in ``_enrich_one_org`` cover
            # LLM/HTTP failures; ``KeyboardInterrupt``/``SystemExit``/
            # ``CancelledError`` (BaseException) propagate to
            # ``asyncio.run`` so operator Ctrl-C aborts cleanly.
            return await asyncio.gather(*[
                _enrich_one_org(llm, board, slug, jobs, semaphore=semaphore)
                for board, slug, jobs in work_items
            ])

        results = asyncio.run(_drive())
        for _slug, status, _path, _payload in results:
            if status == "ok":
                succeeded += 1
            else:
                failed += 1

    print(
        f"[enrich] LLM summary: succeeded={succeeded} failed={failed}",
        flush=True,
    )

    # ============== Phase 3: rebuild _skip_list per board =============
    for board in boards:
        slugs_to_skip = _build_skip_list(board)
        _write_skip_list(board, slugs_to_skip)
        print(
            f"[enrich] skip list for {board}: {len(slugs_to_skip)} slugs -> "
            f"{PROFILE_DIR / board / '_skip_list.json'}",
            flush=True,
        )

    print(
        "[enrich] DONE. To activate skip-list gating in the boards runner, "
        "set BOARDS_USE_ENRICHED_PROFILES=1 in the GHA workflow env. "
        "Default 0 preserves the existing behavior.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
