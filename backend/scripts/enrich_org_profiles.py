"""One-time LLM enrichment for orgs across the 3 ATS boards.

Why this exists
===============

The boards scanner runs hourly on GHA and burns LLM tokens on every
fresh opportunity, even opportunities at orgs that are obviously
non-tech, sponsorship-closed, or haven't posted in 6+ months. This
script runs **once** to classify every org into a structured
``OrgProfile`` JSON, then routes each org into a per-cadence
subdirectory on disk. The boards runner consults those subdirectories
on every cron tick via the ``BOARDS_CADENCES`` env var so each
cadence bucket can run at a different schedule.

Output layout (per board)
-------------------------

    data/enriched/<board>/cadence/<bucket>/<slug>.json
        # status="ok" profile, bucketed by posting_cadence.
        # bucket ∈ {daily, few_per_week, weekly, biweekly, monthly,
        #           quarterly, rare, dead, unknown}.
    data/enriched/<board>/skip/<slug>.json
        # status="ok" profile for an org disqualified by Rule 1
        # (sponsorship/clearance block) or Rule 2 (confidently
        # non-tech with high LLM confidence). Never scanned.
    data/enriched/<board>/errors/<slug>.json
        # status="failed" or status="skipped" envelope — transient
        # LLM/parse failures, fewer-than-MIN_JOBS_FOR_LLM jobs,
        # empty fetches, etc. Operator-inspection only; the
        # boards runner never reads these.

The expensive LLM call (one per org) happens once. The cheap
per-board directory walk happens on every cron — the boards runner
reads slugs as filenames (no JSON parse for the cron-time index),
keeping the per-tick overhead well under 1 ms even at the 10K-org
scale.

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
``{status: failed, error: "..."}`` files into
``data/enriched/<board>/errors/<slug>.json``. The per-cadence
router ignores any envelope whose ``status != "ok"``, so a
partial run leaving 50% orgs as ``status: failed`` does not
pollute cadence buckets — those orgs simply never appear in any
schedule's slug list.

Schema versioning
=================

``SCHEMA_VERSION`` bumps whenever the wire shape of
``data/enriched/<board>/<bucket>/<slug>.json`` changes. v2 stayed
through the per-cadence layout change because the on-disk shape
of each individual profile JSON is unchanged — only its
**directory** moved. The skip-list meta file
(``_skip_list.json``) was REMOVED entirely; the boards runner no
longer reads or writes a flat per-board skip list.

Per-cadence scan wiring
=======================

The boards runner reads ``BOARDS_CADENCES`` at scan time and
restricts :func:`pipeline.nodes.jobs_boards.runner.load_orgs`
to the union of slugs found under each named cadence subdir.
The GHA workflow ``.github/workflows/boards-scan.yml`` wires that
variable on a per-tier basis:

    scan-active   (cron "0 * * * *")   BOARDS_CADENCES="daily,few_per_week,weekly,biweekly"
    scan-dormant  (cron "20 2 * * *")  BOARDS_CADENCES="monthly,quarterly,unknown"
    scan-probe    (cron "20 4 * * 0")  BOARDS_CADENCES="rare,dead"

The signal-to-noise on the weekly probe (rare + dead) is
deliberately separate from the hourly active scan: dead orgs
that recover (post again) get re-classified on the next
re-enrich, and rare orgs are unlikely to add new postings
faster than weekly. Operators who want to re-tune the cadence
mapping can edit the per-job ``env:`` block in the workflow
file — no script change required.
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
from datetime import datetime, timezone
from pathlib import Path

import httpx

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

# Schema version — bump when the wire shape of an individual
# on-disk profile JSON changes (OrgProfile field additions,
# SourceJob field additions, Pydantic validators).
#
# v2 stays current through the per-cadence layout move because
# the profile JSON shape itself didn't change — only the
# containing directory moved. The skip-list meta file
# (``_skip_list.json``) was REMOVED at the same time; the
# boards runner's old meta reader didn't get a fallback because
# every board in production is going to have a re-enrich pass
# that produces the new cadence layout before any GHA cron
# reads it.
SCHEMA_VERSION = 2

# Skip rules — kept inline (no env override) so the behavior is
# one-line interpretable. Tunable later if the operator measures
# false-positive or false-negative skip rates.
#
# The original Rule 1 (``posting_cadence in {dead, rare}`` AND
# raw ``source_last_published`` older than
# ``SKIP_CADENCE_STALE_DAYS``) was REMOVED when the on-disk
# layout moved to per-cadence subdirectories. The new layout
# drops a ``dead``-cadence org into
# ``data/enriched/<board>/cadence/dead/<slug>.json`` regardless
# of how long ago it last posted — the GHA probe workflow's
# weekly cron handles the staleness budget naturally, and
# orgs that recover (post again) are picked up on the next
# re-enrich without the operator having to clear a skip list.
# The remaining two rules are orthogonal to cadence so they
# route to the per-board ``skip/`` directory.
MIN_JOBS_FOR_LLM = 3
SKIP_TECH_RATIO_THRESHOLD = 0.15
SKIP_CONFIDENCE_THRESHOLD = 0.7

# Per-cadence buckets the LLM may produce. Every value here
# becomes a subdirectory under ``data/enriched/<board>/cadence/``.
# Empty / unrecognized cadence values are coerced to ``"unknown"``
# inside :func:`_bucket_for_ok_profile` so the runner never sees
# a profile written outside this set.
CADENCE_BUCKETS = frozenset({
    "daily",
    "few_per_week",
    "weekly",
    "biweekly",
    "monthly",
    "quarterly",
    "rare",
    "dead",
    "unknown",
})

# Concurrency — mirrors boards_runner.MAX_WORKERS for fetches and
# boards_scan._score_all for the LLM signature.
MAX_FETCH_WORKERS = 8
# ``LLM_CONCURRENCY`` is env-driven so an operator with a higher-tier
# NVIDIA key (or one willing to accept the cascade risk on a free-tier
# key) can opt up without changing code. Default 2 keeps the in-flight
# request count small enough that the 8-thread fetch above does NOT
# race past NVIDIA's per-key 40 RPM ceiling (2 keys × 40 = 80 RPM
# combined). Higher values (4–8) genuinely work *only* if the operator
# paired this with ``max_retries=0`` on the AsyncOpenAI client in
# :mod:`services.llm_client` to remove the SDK's silent 429 retries.
# See the diagnostic print at :func:`_log_resolved_llm_config` for
# the cascade-aware rationale.
try:
    _LLM_CONCURRENCY_ENV: int = int(os.environ.get("LLM_CONCURRENCY", "2").strip() or "2")
except ValueError:
    _LLM_CONCURRENCY_ENV = 2
# Upper clamp ``max(1, …, 32)`` so a typo (e.g. ``LLM_CONCURRENCY=9999``)
# doesn't silently re-introduce the cascade this knob exists to prevent.
# 32 is well above the operator-side use-case (1-8 typical) and matches
# the original hardcoded ``MAX_FETCH_WORKERS`` scale; the cap is
# enforced here rather than relying on the LLMClient's ``AsyncTokenBucket``
# to do all the work because huge fan-out spends memory on asyncio
# frames before the bucket can pace them down.
LLM_CONCURRENCY: int = max(1, min(32, _LLM_CONCURRENCY_ENV))

# ``sdk_retries`` mirrors the value passed to ``AsyncOpenAI(..., max_retries=N)``
# inside ``services.llm_client``. Keeping it as a module constant makes the
# startup diagnostic able to report it without re-reading the SDK source.
# Bump in tandem with changes to ``LLMClient.__init__``.
LLM_SDK_MAX_RETRIES: int = 0

# LLM input caps. Boards fetcher returns up to a few hundred jobs
# per org; we cap to keep the prompt under ~2.5K input tokens so
# NVIDIA/Groq are both cheap.
MAX_JOBS_TO_LLM = 30
MAX_DESCRIPTION_CHARS = 600
# On-disk source-jobs cap. Distinct from ``MAX_DESCRIPTION_CHARS``
# because the LLM-prompt path wants to keep input tokens tight
# while the on-disk path wants the operator to see the full body
# of the posting when they ``jq`` a profile. 4000 chars comfortably
# fits a typical Greenhouse job's responsibilities + qualifications
# section; full-body retention beyond that is rare.
# Disk budget (per-org): 30 jobs × ~4100 json-escaped bytes ≈
# 125 KB; plus the OrgProfile metadata envelope ≈ 5 KB. Total
# ≈ 130 KB/org. Across the *full* sweep of all three boards
# (10K orgs each, 30K total) the cumulative cost is roughly
# **~4 GB** of new disk on ``data/enriched/`` — comparable to a
# medium-sized data dump, fine for Render paid instances / Oracle
# Always Free (200 GB quota), but worth knowing because GitHub's
# GHA ephemeral runner has a 14 GB total quota and the
# ``backend/data/`` directory is NOT in ``.gitignore`` for the
# jobradar repo. Operators who care about repo size should add
# ``data/enriched/<board>/**/*.json`` to ``.gitignore`` (reproducible
# enrichment outputs and the per-cadence summary in the GHA
# workflow don't need version control).
SOURCE_DESCRIPTION_MAX_CHARS = 4000

logger = logging.getLogger("jobradar.enrich")


# ---------------------------------------------------------------------------
# On-disk schema
# ---------------------------------------------------------------------------
class SourceJob(BaseModel):
    """A single job as captured during the enrichment pass.

    Mirrors the trimmed payload the LLM sees (``title`` / ``description`` /
    ``location`` / ``first_published``) PLUS ``url`` so a downstream
    consumer can deep-link the role without re-running the board
    fetch. Description is bounded to ``MAX_DESCRIPTION_CHARS`` — the
    same cap ``_build_source_jobs`` applies — so the on-disk JSON
    size stays predictable (~5-15 KB per org with 30 jobs).

    Fields are deliberately minimal: anything the LLM did NOT reason
    over (e.g. ``required_skills`` parsed out of the description, the
    ATS-native ``absolute_url`` shape differences, Greenhouse's
    ``metadata`` array of department tags) is intentionally dropped
    here so the schema doesn't drift if we later swap fetchers. Add
    fields here ONLY if the runner or a downstream consumer actually
    reads them.
    """

    title: str
    description: str
    location: str = ""
    first_published: str = ""
    url: str = ""


class OrgProfile(BaseModel):
    """Per-org LLM classification.

    Single source of truth for the fields written to every
    ``data/enriched/<board>/{cadence/<bucket>|skip}/<slug>.json``
    file. ``status`` is stamped on by
    :func:`_enrich_one_org` AFTER Pydantic validation so the
    on-disk shape is unambiguous (``status: "ok"`` for valid
    profiles, ``status: "failed"|"skipped"`` for envelopes in
    ``errors/``).

    Pydantic enforces field types and ranges on write so a
    malformed LLM response lands in
    ``{"status": "failed", "error": ...}`` instead of corrupting
    the JSON contract.
    """

    schema_version: int = SCHEMA_VERSION
    slug: str
    board: str
    enriched_at: str
    source_jobs_count: int
    source_last_published: str | None = None

    # Raw job context the LLM saw during classification. v2+ on-disk
    # profiles carry up to MAX_JOBS_TO_LLM entries; consumers that
    # want the *full* board re-fetch from the active ATS endpoint.
    # Description-truncation matches the LLM-visible trim (see
    # ``_build_source_jobs``) so on-disk size mirrors prompt-input
    # size; no surprise-large-JSONs.
    source_jobs: list[SourceJob] = Field(default_factory=list)

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
    "in the answers above, including how much signal the input has>\n"
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

    Contract pinned by ``TestTrimJobsForPrompt``: the returned dicts
    contain EXACTLY the four keys ``{title, description, location,
    first_published}``. ``url`` is intentionally absent here — the
    LLM doesn't need it, and adding it would push the prompt past
    the input-trim budget on the long-tail orgs. ``url`` IS
    persisted on the on-disk profile via ``_build_source_jobs``.
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


def _build_source_jobs(jobs: list[dict]) -> list[SourceJob]:
    """Build the ``source_jobs`` list persisted into the on-disk profile.

    Mirrors :func:`_trim_jobs_for_prompt`'s field selection so the
    LLM sees exactly what later consumers see (one source of truth
    for the trimmed shape). Adds ``url`` so a downstream consumer —
    the React JobDetail view, a future post-mortem tool, anything
    reading the on-disk JSON — can deep-link the role without
    re-fetching the board.

    Description is truncated to ``MAX_DESCRIPTION_CHARS`` (same cap
    the LLM trim applies) so on-disk size mirrors prompt-input size.
    Greenhouse's ``content`` field is HTML in some orgs — Pydantic
    coerces it to ``str`` via :class:`SourceJob.description`'s
    declaration; the HTML tags land in on-disk JSON verbatim. A
    future ``--strip-html`` flag could clean that up; not done here
    because the LLM classification prompt sees the same HTML and
    the operators reading the on-disk JSON can pick out what they
    need.

    ``location`` is stringified defensively: Greenhouse returns a
    plain string ("Remote US"), Lever returns a string, Ashby
    sometimes returns a ``{"name": "...", "country": "..."}`` dict.
    ``str(dict)`` produces a noisy but truthful repr like
    ``"{'name': 'Remote', 'country': 'US'}"``; consumers that want
    normalized location fields can add a structured field later.

    Capped at ``MAX_JOBS_TO_LLM`` so the persisted list matches the
    LLM-visible cardinality. A profiling export that wants the full
    board should re-fetch rather than rely on this list.
    """
    out: list[SourceJob] = []
    for j in jobs[:MAX_JOBS_TO_LLM]:
        title = j.get("title") or ""
        # ``SOURCE_DESCRIPTION_MAX_CHARS`` (4000) intentionally LARGER
        # than the LLM's ``MAX_DESCRIPTION_CHARS`` (600) — the prompt
        # path wants tight token budget; the on-disk path wants the
        # operator to see the full body. Without this split, every
        # org-profile JSON has truncated 600-char descriptions that
        # cut off qualification sections.
        description = (
            j.get("description") or j.get("content") or ""
        )[:SOURCE_DESCRIPTION_MAX_CHARS]
        # Ashby's posting API returns ``location`` as a nested dict
        # ``{"name": "Remote", "country": "US"}``; Greenhouse + Lever
        # return a plain string. ``str(dict)`` would produce a noisy
        # repr (``"{'name': 'Remote', 'country': 'US'}"``) that
        # breaks simple ``jq .location`` downstream. Prefer the
        # dict's ``name`` field. When ``name`` is absent (Ashby
        # sometimes returns only ``country`` / ``city``), fall
        # back to a compact "country=US" / "city=Foo,country=US"
        # form rather than empty string — the operator gets *some*
        # signal to filter on instead of an indistinguishable
        # empty value. ``sorted`` the non-name keys so the on-disk
        # wire format is stable across ATS responses that emit
        # dict keys in different orders — downstream ``grep`` and
        # ``jq --arg`` queries can rely on the format.
        location_raw = j.get("location")
        if isinstance(location_raw, dict):
            name = location_raw.get("name")
            if name:
                location = str(name)
            else:
                non_name = [
                    f"{k}={v}"
                    for k, v in sorted(location_raw.items())
                    if k != "name" and v
                ]
                location = ",".join(non_name)
        elif isinstance(location_raw, str):
            location = location_raw
        else:
            location = ""
        published = j.get("published_at") or ""
        if hasattr(published, "isoformat"):
            published = published.isoformat()
        url = j.get("url") or ""
        out.append(
            SourceJob(
                title=title,
                description=description,
                location=location,
                first_published=published,
                url=url,
            )
        )
    return out


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

    Used to populate ``source_last_published`` so the on-disk
    profile carries a raw timestamp independent of the LLM's
    ``posting_cadence`` derivation. Operators reading the per-cadence
    bucket can spot a "dead" org that recently started posting
    again by sorting on ``source_last_published`` even when the
    LLM hasn't been re-run yet.
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
    ``status: failed``) into ``data/enriched/<board>/errors/<slug>.json``.

    The on-disk layout treats ``errors/`` as a sibling of
    ``skip/`` and ``cadence/<bucket>/`` — the boards runner
    consults only :data:`CADENCE_BUCKETS` for active scans, so a
    slug with status ``failed`` or ``skipped`` is invisible to
    the runner regardless of how it landed in ``errors/``.
    Operators reading the directory during a post-mortem can
    enumerate failures by globbing ``errors/*.json`` and reading
    the ``reason`` field.

    The function also calls
    :func:`_purge_stale_duplicate_profiles` so a re-enrich that
    previously had a successful profile at a cadence bucket or
    in ``skip/`` but failed on this attempt cleans up the stale
    copy first; the on-disk invariant is "exactly one profile
    per (board, slug) tuple".
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
    out = PROFILE_DIR / board / "errors" / f"{slug}.json"
    _purge_stale_duplicate_profiles(
        board=board, slug=slug, target_path=out,
    )
    _atomic_write_json(out, payload)
    return out


# ---------------------------------------------------------------------------
# Per-cadence bucket routing (Phase 3 plumbing)
# ---------------------------------------------------------------------------
def _compute_skip_for_profile(
    profile: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when the org should land in the per-board
    ``skip/`` directory — i.e. should NEVER be scanned at any
    cadence.

    Rules — any one is sufficient:
    1. ``sponsorship_open is False`` (explicit sponsor-block) OR
       ``clearance_required is True`` (DOD/IC/TS-SCI). The
       ``None`` sponsor-open sentinel does NOT trigger skip —
       "we couldn't tell" is not a "yes, blocked" signal.
    2. ``tech_role_ratio < SKIP_TECH_RATIO_THRESHOLD`` AND
       ``overall_confidence > SKIP_CONFIDENCE_THRESHOLD`` —
       conjunction (NOT ``>=``) so a borderline LLM call still
       falls through to current regex scoring rather than
       silently dropping the org.

    Returns False unconditionally for profiles whose
    ``status != 'ok'`` — failed/skipped envelopes never leak
    into the ``skip/`` directory; they go into
    ``errors/<slug>.json`` instead.

    Note: the original Rule 1 (``posting_cadence in {dead, rare}``
    AND raw-age > ``SKIP_CADENCE_STALE_DAYS``) was REMOVED when
    the on-disk layout moved to per-cadence subdirectories — a
    ``dead``-cadence org now lands in
    ``data/enriched/<board>/cadence/dead/<slug>.json`` regardless
    of how long ago it last posted; the GHA probe workflow's
    weekly cron handles the staleness budget naturally, and
    orgs that recover (post again) are picked up on the next
    re-enrich without the operator having to clear a skip list.

    ``now`` defaults to ``datetime.now(timezone.utc)`` so callers
    that don't care about staleness (the only ``now`` consumer
    was Rule 1, now removed) can omit it. Production callers in
    :func:`_enrich_one_org` don't pass ``now`` because the LLM-
    visible timestamp is irrelevant to the post-Rule-1 logic.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(profile, dict) or profile.get("status") != "ok":
        return False
    # Rule 1: explicit sponsor-block OR US security clearance
    # required. Both are disqualifying per the operator's stated
    # GHA optimization: orgs that hard-block visa sponsorship
    # OR require DOD/IC/TS-SCI eligibility burn LLM tokens +
    # GHA wall-clock for zero applied yield.
    # Note: pure US-citizenship-only (no security clearance)
    # postings surface as ``sponsorship_open=False`` already,
    # so they fall under the first branch of this OR. This
    # branch is for the orthogonal DOD/IC subset.
    if profile.get("sponsorship_open") is False or profile.get("clearance_required") is True:
        return True
    # Rule 2: confidently non-tech
    tech_ratio = profile.get("tech_role_ratio")
    confidence = profile.get("overall_confidence")
    if tech_ratio is not None and confidence is not None:
        if (
            tech_ratio < SKIP_TECH_RATIO_THRESHOLD
            and confidence > SKIP_CONFIDENCE_THRESHOLD
        ):
            return True
    return False


def _bucket_for_ok_profile(profile: dict) -> str:
    """Return the on-disk bucket name for a ``status: ok`` profile.

    Returns one of:
    * ``"skip"`` — never scanned (Rule 1 or Rule 2 matched).
    * one of :data:`CADENCE_BUCKETS` — the LLM-derived
      ``posting_cadence`` coerced into the canonical set.

    Profiles whose ``posting_cadence`` falls outside
    :data:`CADENCE_BUCKETS` are coerced to ``"unknown"`` so the
    runner sees a deterministic bucket name regardless of LLM
    drift. The ``"unknown"`` bucket lands in the dormant tier's
    daily cron by default — operators can re-tune by mapping it
    into a different GHA workflow tier.
    """
    if _compute_skip_for_profile(profile):
        return "skip"
    cadence = (profile.get("posting_cadence") or "").strip().lower()
    if cadence in CADENCE_BUCKETS:
        return cadence
    return "unknown"


def _target_path_for_slug(*, board: str, slug: str, bucket: str) -> Path:
    """Return the canonical on-disk path for an OK profile in ``bucket``.

    ``"skip"`` -> ``data/enriched/<board>/skip/<slug>.json``.
    Otherwise ``data/enriched/<board>/cadence/<bucket>/<slug>.json``.

    Errors envelopes use a different path built directly inside
    :func:`_write_status_envelope` (``errors/<slug>.json``).
    """
    if bucket == "skip":
        return PROFILE_DIR / board / "skip" / f"{slug}.json"
    return PROFILE_DIR / board / "cadence" / bucket / f"{slug}.json"


def _stale_profile_paths_for_slug(*, board: str, slug: str) -> list[Path]:
    """All on-disk locations a slug might already occupy, in delete order.

    Walks the legacy top-level ``<slug>.json`` location, the
    ``errors/`` location, the ``skip/`` location, and every cadence
    bucket. The caller filters out the location it's about to
    write to and unlinks the rest, so re-enrichment physically
    moves a slug from old cadence bucket -> new cadence bucket
    instead of leaving a stale duplicate at the old location.
    """
    board_dir = PROFILE_DIR / board
    candidates: list[Path] = [
        # Legacy top-level file from the v1/v2 layout — exists on
        # disk whenever this script ran BEFORE the per-cadence
        # layout shipped. Operator confirmed the prior enrichment
        # was incomplete (replied "the enrichment is not complete
        # yet" to the migration question), so we delete-as-we-go
        # rather than carry a separate migration flag — the next
        # re-enrich overwrites + cleans out as it runs.
        board_dir / f"{slug}.json",
        board_dir / "errors" / f"{slug}.json",
        board_dir / "skip" / f"{slug}.json",
    ]
    candidates.extend(
        board_dir / "cadence" / bucket / f"{slug}.json"
        for bucket in sorted(CADENCE_BUCKETS)
    )
    return candidates


def _purge_stale_duplicate_profiles(
    *, board: str, slug: str, target_path: Path
) -> list[Path]:
    """Delete every on-disk duplicate of ``slug`` EXCEPT
    ``target_path``.

    Called by :func:`_enrich_one_org` (after a successful re-LLM)
    and :func:`_write_status_envelope` (after writing an
    errors envelope) so the on-disk state always converges to
    one profile per (board, slug) pair. A re-enrich that flips
    the cadence bucket moves the file physically rather than
    leaving a stale copy at the old location.
    """
    removed: list[Path] = []
    for path in _stale_profile_paths_for_slug(board=board, slug=slug):
        if path == target_path:
            continue
        if not path.exists():
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            logger.warning(
                "could not purge stale duplicate %s for %s/%s: %s",
                path, board, slug, exc,
            )
    return removed


def _find_existing_profile(*, board: str, slug: str) -> Path | None:
    """Return the existing on-disk profile path for ``slug`` if any.

    Used by Phase 1's ``--skip-existing`` gate so a profile that
    landed in a non-trivial bucket (say
    ``cadence/weekly/<slug>.json`` under the new layout, OR the
    legacy ``data/enriched/<board>/<slug>.json`` if the prior
    enrichment was incomplete) still counts as "already
    enriched". Returns ``None`` when the slug has not been
    enriched yet.
    """
    for path in _stale_profile_paths_for_slug(board=board, slug=slug):
        if path.exists():
            return path
    return None


def _emit_per_bucket_summary(*, board: str) -> None:
    """Phase 3: emit a per-bucket slug-count line for ``board``.

    Walks every cadence bucket plus ``skip/`` and ``errors/`` and
    prints a single line per populated bucket. The boards runner
    no longer consults a flat ``_skip_list.json``; this summary
    is the operator's at-a-glance confirmation that the cadence
    layout did what it should on this run.

    Empty buckets are omitted from output (a bucket that scores
    zero organisations is noise; the line would obscure the
    populated ones during spot-check).
    """
    board_dir = PROFILE_DIR / board
    labels: list[tuple[str, Path]] = [
        ("skip", board_dir / "skip"),
        ("errors", board_dir / "errors"),
    ]
    labels.extend(
        (f"cadence/{b}", board_dir / "cadence" / b)
        for b in sorted(CADENCE_BUCKETS)
    )
    for label, path in labels:
        if not path.exists():
            continue
        count = sum(1 for _ in path.glob("*.json"))
        if count:
            print(
                f"[enrich] bucket {board}/{label}: {count} slugs",
                flush=True,
            )


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

    Returns ``(slug, status, path, payload)`` where ``status`` is
    one of ``"ok"``, ``"skipped"``, ``"failed"``. ``path`` always
    points at the on-disk file so the caller can log it.

    Errors are caught and translated to a ``status: failed``
    envelope — the run never crashes mid-loop because one org's
    LLM call returned malformed JSON.

    Concurrency contract
    ---------------------

    This is a single ``async def`` coroutine, called from one
    outer ``asyncio.run(...)`` block in :func:`main`. The
    ``semaphore`` parameter is created in the same outer loop
    and ``async with`` works against it normally. Do NOT wrap
    this in ``asyncio.to_thread`` — the inner ``asyncio.run``
    would create a second loop and ``asyncio.Semaphore`` is
    bound to the loop where it was created (the cross-loop
    access raises ``RuntimeError``). The LLMClient's internal
    ``AsyncTokenBucket`` has the same constraint — multiple
    loops fighting for one ``asyncio.Lock`` break. Single-loop
    async is the design.
    """
    sys_prompt = ENRICHMENT_SYSTEM_PROMPT
    user_prompt = _build_user_prompt(board, slug, jobs)
    latest = _latest_iso_published(jobs)
    # Build the on-disk ``source_jobs`` list NOW so we can hand
    # it to the OrgProfile constructor as a single
    # Pydantic-validated field. Building AFTER the LLM call
    # would force a second iteration over ``jobs``; building
    # BEFORE saves the small loop and keeps the trim logic
    # single-sourced (``_build_source_jobs`` uses the same
    # MAX_JOBS_TO_LLM / MAX_DESCRIPTION_CHARS caps as
    # ``_trim_jobs_for_prompt``).
    source_jobs = _build_source_jobs(jobs)

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
            source_jobs=source_jobs,
            model_used=model,
            **parsed,
        )
        # Stamp ``status: "ok"`` on the persisted payload so
        # :func:`_bucket_for_ok_profile` classifies on the same
        # key the race-path check (the ``status != "ok"``
        # short-circuit at the top of
        # ``_compute_skip_for_profile``) uses. Without this
        # field, ``_compute_skip_for_profile`` sees the
        # bare-profile envelope and never returns True, so
        # every profile would land in
        # ``cadence/<posting_cadence>/`` — including sponsorship
        # blocks that should have routed to ``skip/``.
        payload = profile.model_dump()
        payload["status"] = "ok"
        # Compute the on-disk bucket BEFORE any I/O so the
        # duplicate-purge call below sees the right
        # ``target_path``. A re-enrich that flips the LLM's
        # ``posting_cadence`` from ``daily`` to ``weekly`` (or
        # anything else in :data:`CADENCE_BUCKETS`) ends up here
        # with a different target; the purge call deletes the
        # old bucket's copy so the on-disk invariant holds.
        bucket = _bucket_for_ok_profile(payload)
        out_path = _target_path_for_slug(
            board=board, slug=slug, bucket=bucket,
        )
        _purge_stale_duplicate_profiles(
            board=board, slug=slug, target_path=out_path,
        )
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
        # ``_PermanentError`` from ``services.llm_client``
        # (parse failure, all-providers-failed), HTTP timeout,
        # anything else — log+continue via the envelope, never
        # crash the loop.
        path = _write_status_envelope(
            board=board,
            slug=slug,
            status="failed",
            reason_or_error=f"{type(exc).__name__}: {exc}"[:1000],
        )
        return slug, "failed", path, {}


def _fetch_one(fetcher, board: str, slug: str, client) -> tuple[str, list[dict]]:
    """Synchronous fetcher call wrapped for ThreadPoolExecutor.

    Returns ``(slug, jobs)``. Non-OK outcomes (HTTP 4xx, 5xx up
    to the retry budget, network blips) return ``(slug, [])`` so
    the orchestrator writes a ``status: failed`` envelope and
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
# Startup observability — surface misconfigured LLM env before a run burns
# tokens on a model id NVIDIA's catalogue doesn't know. Triggered
# unconditionally (including ``--dry-run``) so an operator sees the
# resolved config without reading the source.
# ---------------------------------------------------------------------------
def _log_resolved_llm_config() -> None:
    """Print the resolved LLM env values so the operator can audit them at start.

    Mirrors the same env-key order and defaulting language as
    :meth:`services.llm_client.LLMClient.from_env` so the diagnostic
    matches what the chain will actually use. Empty-string values
    are coerced to the in-code defaults (matching
    :meth:`from_env`'s own behaviour) rather than passed through as
    ``""`` which would cause a ``BadRequest`` / 401 on the first
    chat-completion.
    """
    # Late import keeps the top-of-file dependency surface small
    # and matches the pattern used by other helpers in this
    # script that pull from ``services.llm_client`` lazily.
    from services.llm_client import (
        DEFAULT_GROQ_MODEL,
        DEFAULT_NVIDIA_BASE,
        DEFAULT_NVIDIA_MODEL,
        DEFAULT_NVIDIA_RPM,
    )

    # Read raw values exactly as :meth:`LLMClient.from_env` will
    # see them — no ``.strip() or DEFAULT`` silent fallback.
    # ``os.environ.get("X", DEFAULT)`` returns DEFAULT only when
    # the key is *absent*, not when it's present-but-empty
    # (``NVIDIA_MODEL=``); an empty value forwards ``""`` to
    # AsyncOpenAI and produces a 401 on the first chat-completion.
    # The diagnostic deliberately surfaces that mismatch.
    nvidia_model = os.environ.get("NVIDIA_MODEL", DEFAULT_NVIDIA_MODEL)
    nvidia_base = os.environ.get("NVIDIA_BASE_URL", DEFAULT_NVIDIA_BASE)
    raw_rpm = os.environ.get("NVIDIA_RPM", str(DEFAULT_NVIDIA_RPM))
    rpm_note = ""
    try:
        # Mirror :meth:`LLMClient.from_env` ``int(raw_rpm or DEFAULT)``:
        # empty -> DEFAULT_NVIDIA_RPM (silent), malformed ->
        # ValueError -> raise ``ValueError`` at import (we surface
        # as -1 + inline note).
        nvidia_rpm = int(raw_rpm.strip() or DEFAULT_NVIDIA_RPM)
    except ValueError:
        nvidia_rpm = -1
        # Show the raw bad value alongside the sentinel so the
        # operator staring at the diagnostic sees *what* failed
        # without having to grep their shell history.
        rpm_note = f" (raw={raw_rpm!r} UNPARSEABLE — LLMClient.from_env will raise ValueError at import)"
    groq_model = os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)

    print(
        "[enrich] resolved LLM config: "
        f"NVIDIA_MODEL={nvidia_model!r} "
        f"NVIDIA_BASE_URL={nvidia_base!r} "
        f"NVIDIA_RPM={nvidia_rpm} "
        f"GROQ_MODEL={groq_model!r}",
        flush=True,
    )

    # Runtime-computed PACE LIMIT — same math as
    # :meth:`services.llm_client.LLMClient.from_env` which builds
    # the NVIDIA bucket: ``total_rpm = rpm_per_key * len(non-empty
    # NVIDIA_API_KEY / _2)``. Compute ``key_suffix`` BEFORE the
    # print so pluralization is human-readable; mirror the same
    # ``os.environ.get("X", "").strip()`` read so the count
    # matches what the bucket sees at request time. Operator with
    # two NVIDIA keys and ``NVIDIA_RPM=40`` should see ``2 NIM
    # keys * 40 RPM/key = 80 RPM total``; if they see ``1 NIM key
    # * 40 RPM/key = 40 RPM total`` the second key is unset and
    # they're at half their expected budget.
    nvidia_keys = [
        name for name in ("NVIDIA_API_KEY", "NVIDIA_API_KEY_2")
        if os.environ.get(name, "").strip()
    ]
    nvidia_key_count = len(nvidia_keys)
    key_suffix = "s" if nvidia_key_count != 1 else ""
    rpm_per_key_str = str(nvidia_rpm) if nvidia_rpm >= 0 else "?"
    runtime_total_rpm = nvidia_rpm * nvidia_key_count if nvidia_rpm >= 0 else 0
    print(
        "[enrich] pacing picture: "
        + str(nvidia_key_count) + " NIM key" + key_suffix
        + " * " + rpm_per_key_str + " RPM/key"
        + " = " + str(runtime_total_rpm) + " RPM total "
        + "(LLM_CONCURRENCY=" + str(LLM_CONCURRENCY)
        + ", sdk_retries=" + str(LLM_SDK_MAX_RETRIES) + "; "
        + "max in-flight ~= LLM_CONCURRENCY, burst above runtime_total_rpm "
        + "will 429 the gateway).",
        flush=True,
    )


def _verify_nvidia_model_present(*, http_timeout: float = 5.0) -> None:
    """Single ``GET /v1/models`` against NVIDIA to confirm ``NVIDIA_MODEL`` is on the live catalogue.

    Never raises. Failure modes are logged at INFO (the WARN is
    reserved for the actual misconfig case the operator needs to
    act on):

    * ``NVIDIA_API_KEY`` unset → skip the check (operator must opt in).
    * ``GET /v1/models`` returns non-200 → skip + log status code.
    * Network / timeout / JSON parse error → skip + log the exception class.

    The successful path logs one of:
    * ``NVIDIA_MODEL=... confirmed on NVIDIA catalogue (N models-listed).``
    * ``WARN  NVIDIA_MODEL=... is NOT on the live NVIDIA catalogue ...``

    The single GET hits only the catalogue endpoint (no
    chat-completions spend), so it's safe to run on every
    invocation including cron.
    """
    from services.llm_client import (
        DEFAULT_NVIDIA_BASE,
        DEFAULT_NVIDIA_MODEL,
    )

    # Same raw read as :func:`_log_resolved_llm_config` — no
    # ``.strip() or DEFAULT`` coercion; the catalogue check then
    # matches the empty-string id against the catalogue (which it
    # won't be) and raises the WARN, mirroring what will happen
    # on the first chat call.
    nvidia_model = os.environ.get("NVIDIA_MODEL", DEFAULT_NVIDIA_MODEL)
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        print(
            "[enrich] NVIDIA catalogue check skipped: NVIDIA_API_KEY is unset.",
            flush=True,
        )
        return
    base = os.environ.get("NVIDIA_BASE_URL", "").strip() or DEFAULT_NVIDIA_BASE
    url = f"{base.rstrip('/')}/models"
    try:
        with httpx.Client(timeout=http_timeout) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            print(
                f"[enrich] NVIDIA catalogue check skipped: GET {url} returned "
                f"HTTP {resp.status_code}.",
                flush=True,
            )
            return
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            print(
                f"[enrich] NVIDIA catalogue check skipped: non-JSON reply — "
                f"{type(exc).__name__}.",
                flush=True,
            )
            return
    except httpx.HTTPError as exc:
        print(
            f"[enrich] NVIDIA catalogue check skipped: HTTP error "
            f"{type(exc).__name__} reaching {url}.",
            flush=True,
        )
        return
    except Exception as exc:  # noqa: BLE001 — guard rail, never crash
        print(
            f"[enrich] NVIDIA catalogue check skipped: "
            f"{type(exc).__name__} reaching {url}.",
            flush=True,
        )
        return

    catalogue = data.get("data") if isinstance(data, dict) else None
    if not isinstance(catalogue, list):
        print(
            "[enrich] NVIDIA catalogue check skipped: unexpected response "
            "shape (no .data list).",
            flush=True,
        )
        return
    ids = {m.get("id") for m in catalogue if isinstance(m, dict) and isinstance(m.get("id"), str)}
    if nvidia_model in ids:
        print(
            f"[enrich] NVIDIA_MODEL={nvidia_model!r} confirmed on NVIDIA catalogue "
            f"({len(ids)} models-listed).",
            flush=True,
        )
    else:
        print(
            f"[enrich] WARN  NVIDIA_MODEL={nvidia_model!r} is NOT on the live NVIDIA "
            f"catalogue ({len(ids)} models-listed). Every NVIDIA call will 404 until "
            f"NVIDIA_MODEL is set to a current id (e.g. "
            f"'meta/llama-3.1-70b-instruct' or 'meta/llama-3.3-70b-instruct').",
            flush=True,
        )


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
        help="Compute per-bucket summary (Phase 3) but skip Phase 1 fetches + Phase 2 LLM calls.",
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
    # Startup observability BEFORE any fetch / LLM work — a bad
    # NVIDIA_MODEL id will hit 404 on every call otherwise. Both
    # helpers are guard rails: they print to stdout (via ``print``,
    # not :data:`logger`, so the lines appear even when the rest
    # of the script is bufferred) and never raise.
    _log_resolved_llm_config()
    _verify_nvidia_model_present()

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
                    # Empty fetch (transient or 404). Treat as
                    # failed ONLY if there's no prior profile —
                    # keep existing data on flaky upstreams rather
                    # than churning on-disk state.
                    # :func:`_find_existing_profile` walks every
                    # on-disk location (legacy top-level,
                    # ``errors/``, ``skip/``, every cadence
                    # bucket) so a slug that landed in any bucket
                    # under a previous enrichment pass counts as
                    # "already on disk".
                    existing = _find_existing_profile(
                        board=board, slug=slug,
                    )
                    if existing is None:
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
                # Same broad reach as the empty-fetch branch
                # above — a slug re-classified from
                # ``cadence/daily`` to ``cadence/weekly`` (or to
                # ``skip/``) on a previous enrichment pass is
                # still "already enriched" by this gate.
                existing = _find_existing_profile(
                    board=board, slug=slug,
                )
                if args.skip_existing and existing is not None and not args.force:
                    try:
                        with open(existing, "r") as f:
                            existing_profile = json.load(f)
                        if existing_profile.get("status") != "ok":
                            # Not ``status: ok`` — failed/skipped
                            # envelopes get rewritten
                            # unconditionally so the on-disk
                            # state converges to ``ok`` after a
                            # successful run. No opt-in needed:
                            # this rewrite doesn't burn LLM
                            # budget (the new run will).
                            pass
                        else:
                            # ``status: ok`` profile on disk.
                            # Default behavior: trust it (skip
                            # re-enrich), regardless of
                            # ``schema_version``. The
                            # description-truncation gap between
                            # v1 and v2 is real but the LLM cost
                            # of blanket re-enriching 10K orgs —
                            # ~$30-50 at NVIDIA free-tier pricing
                            # — is a worse surprise than profile
                            # contents being slightly stale.
                            #
                            # ``ENRICH_MIGRATE_V1_TO_V2=1`` flips
                            # the behavior: it forces a
                            # re-enrich whenever the on-disk
                            # ``schema_version`` doesn't match
                            # ``SCHEMA_VERSION``, so an operator
                            # who actually wants the v1→v2
                            # migration (with descriptions in
                            # source_jobs) can opt in once and
                            # watch the runs roll forward
                            # incrementally. Default-safe — the
                            # env var is opt-in like
                            # ``BOARDS_SKIP_TIMEOUTS`` so a typo
                            # (``ENRICH_MIGRATE_V1_TO_V2=2``)
                            # doesn't silently enable a $30
                            # sweep.
                            #
                            # Truthy string whitelist:
                            # ``"1"``/``"true"``/``"yes"``
                            # (lowered).
                            on_disk_version = existing_profile.get("schema_version")
                            if on_disk_version == SCHEMA_VERSION:
                                skipped_existing += 1
                                continue
                            _migrate_env = os.environ.get(
                                "ENRICH_MIGRATE_V1_TO_V2", "0"
                            ).strip().lower()
                            if _migrate_env not in ("1", "true", "yes"):
                                # Default: preserve existing v1
                                # profile, don't burn LLM. The
                                # ``status == "ok"`` check above
                                # already filtered out
                                # failed/skipped envelopes, so
                                # this branch only sees
                                # healthy-but-stale v1 data.
                                # Log at debug level so an
                                # operator investigating the
                                # skip count can see *why* the
                                # gate fired.
                                logger.debug(
                                    "skip v%d %s/%s profile (set "
                                    "ENRICH_MIGRATE_V1_TO_V2=1 to re-enrich)",
                                    on_disk_version, board, slug,
                                )
                                skipped_existing += 1
                                continue
                            # Explicit opt-in: fall through to
                            # work_items (re-enrich will run).
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
        # In dry-run we still print the per-bucket summary from
        # any existing on-disk profiles so the operator can
        # preview what WOULD change.
        for board in boards:
            _emit_per_bucket_summary(board=board)
        return 0

    # ============== Phase 2: enrich via LLM (bounded concurrency) ========
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
            # All LLM coroutines share the same outer loop, so
            # the semaphore bounds in-flight calls cleanly
            # without a cross-loop ``asyncio.run`` shadow event.
            # ``asyncio.gather`` schedules all
            # ``LLM_CONCURRENCY`` tasks at once; the semaphore
            # gates the actual ``await llm.run_json_prompt``
            # honoring NVIDIA's ``AsyncTokenBucket`` RPM cap
            # below. Standard ``gather`` (no
            # return_exceptions): the inner ``except Exception``
            # blocks in ``_enrich_one_org`` cover LLM/HTTP
            # failures; ``KeyboardInterrupt``/``SystemExit``/
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

    # ============== Phase 3: per-bucket summary per board ================
    # The :func:`_emit_per_bucket_summary` walker reads slugs as
    # filenames (``glob("*.json")``) — no JSON parse — so the
    # per-board summary cost stays under ~1 ms even at the
    # 10K-org scale. Boards runner integration is wired via
    # ``BOARDS_CADENCES`` in the GHA workflow env; the per-bucket
    # counts here are an at-a-glance operator sanity check, not
    # a load-bearing artifact.
    for board in boards:
        _emit_per_bucket_summary(board=board)

    print(
        "[enrich] DONE. To activate per-cadence scan gating in the "
        "boards runner, set BOARDS_CADENCES=<csv> in the GHA workflow "
        "env. Default mapping: "
        "active(hourly)=daily,few_per_week,weekly,biweekly | "
        "dormant(daily 02:20 UTC)=monthly,quarterly,unknown | "
        "probe(weekly Sun 04:20 UTC)=rare,dead. "
        "Without BOARDS_CADENCES, the runner scans every org per "
        "legacy behavior (full board minus missing/timeout orgs).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
