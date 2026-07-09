"""Heuristic role pre-filter — applied after the per-board fetcher returns.

The point of this module is to drop obviously-irrelevant jobs *before*
they reach the LLM scorer so we don't waste tokens on roles the operator
would never consider. Filtering is purely deterministic (regex over
title + optional description) and runs entirely on the FastAPI side.

Three categories of heuristic block implemented here:

* :data:`NO_SPONSORSHIP_PATTERNS` — "no visa / citizenship / work auth
  required" phrases. Expanded from the original 8 to cover common ATS
  phrasings ("must be authorized to work", "permanent resident
  required", "H1B not provided", etc).
* :data:`CLEARANCE_PATTERNS` (new) — government / cleared-only roles
  ("security clearance required", "TS/SCI", "DoD contract", ITAR/EAR,
  etc). Same apply-or-skip contract as visa patterns — hard-drops
  anything matching.
* :data:`SENIORITY_TIERS` (new) — ladder from ``intern`` → ``vp``` with
  per-tier regex. The relevant range is bounded at runtime by the
  ``min_seniority`` / ``max_seniority`` knobs that come from the
  Preferences singleton (see :mod:`routes.settings`).

Seniority classification algorithm
-----------------------------------

:class:`classify_seniority` returns the **highest-rank** tier whose
regex matches the title. "Senior Staff Engineer" matches both "senior"
(rank 3) and "staff" (rank 4), returning ``"staff"``. "Senior Director"
matches both "senior" and "director" — returns ``"director"``. "Lead"
sits at rank 5 (peers with ``principal``) because IC-leadership and
principal are interchangeable in many leveling rubrics; the operator's
band shouldn't surprise on that boundary.

Implicit mid-rank for unclassifiable titles
-------------------------------------------

Titles like ``"Software Engineer"`` (no seniority hint) do not match
any tier in the ladder. The :func:`is_relevant_role` band check treats
those titles as **rank 2** (mid). The reasoning: the operator opted in
to a band, so we default to permissive behaviour — a title without
seniority markup is usually *at least* mid-level (rarely entry-level).
With ``min=mid, max=staff`` an unmarked "Software Engineer" passes;
with ``min=senior`` it fails. Sensible defaults and keeps the operator
in control.

Pipeline order in :func:`is_relevant_role`
-----------------------------------------

1. **Visa / clearance hard drops** — operator hasn't sponsored or
   cleared, so any role demanding either is rejected.
2. **Seniority band** — only fires when *either* bound is set; with
   both ``None`` (the default) this is a no-op.
3. **Positive relevant match wins** — if a title contains a relevant
   keyword, it's kept regardless of any other words.
4. **Negative irrelevant match** — if no relevant keyword matched
   and a "soft negative" did (sales / intern / contract / etc.),
   reject.
5. **Default reject** — neither matched, so reject (better to drop
   than to assume relevance).
"""
from __future__ import annotations

import re
from typing import Iterable, Literal, Optional


DEFAULT_IRRELEVANT_PATTERNS = [
    r"(?i)\b(sales|account executive|business development|customer success|support|operations|finance|hr|human resources|recruiter|marketing|content|design|product manager|product designer|project manager|scrum master|intern|internship|contract|temporary|part-time)\b",
]

DEFAULT_RELEVANT_PATTERNS = [
    r"(?i)\b(software engineer|software engineering|engineer|developer|data engineer|machine learning|ml engineer|platform engineer|backend engineer|frontend engineer|full stack engineer|ai engineer|research engineer|sre|devops|security engineer|infrastructure engineer|site reliability|solutions engineer|applied scientist)\b",
]


# ---------------------------------------------------------------------------
# Visa / work-authorisation blocks. These run against the *combined*
# title + description text so a borderline title like "Software Engineer
# (US Citizens Only)" gets caught by the description.
# ---------------------------------------------------------------------------
NO_SPONSORSHIP_PATTERNS = [
    # Direct "we will not sponsor" phrasing.
    r"(?i)\b(no visa sponsorship|no sponsorship|not eligible for sponsorship|sponsorship not available|cannot sponsor|will not sponsor|cannot provide sponsorship|unable to sponsor)\b",
    # Work-authorisation phrasing.
    r"(?i)\b(no work authorization|work authorization required|must be authorized to work|must have authorization)\b",
    # Citizenship / permanent-residency phrasing.
    r"(?i)\b(citizenship required|must be a us citizen|must be a citizen|us citizenship required|u\.?s\.? citizenship required|permanent resident required|green card required|green card holder)\b",
    # H-1B specific phrasings.
    r"(?i)\b(h1b not provided|h-?1b not provided)\b",
]


# ---------------------------------------------------------------------------
# Clearance / DoD / federal blocks. Same apply-or-skip contract as the
# visa list — hard drop. Always-on by design; the default operator
# profile doesn't have a clearance, so unfiltered cleared roles would
# just produce a queue of "can't apply" entries the LLM then has to
# score out anyway.
# ---------------------------------------------------------------------------
CLEARANCE_PATTERNS = [
    # Generic "must have / active / required" clearance phrasing.
    r"(?i)\b(security clearance(?:\s+required)?|active\s+security\s+clearance|clearance\s+required|must\s+(?:have|possess)\s+(?:a?\s*)?security\s+clearance)\b",
    # Top Secret / SCI — the strongest excluded tier.
    r"(?i)\b(top\s*secret|ts\s*/\s*sci|ts\s*sci|tssci|sci\s+clearance|active\s+ts|active\s+top\s+secret)\b",
    # Other cleared-only phrasing.
    r"(?i)\b(secret\s+clearance|public\s+trust|classified\s+clearance|polygraph\s+(?:required|examination))\b",
    # Trade / arms-control regimes.
    r"(?i)\b(itar|ear\s+(?:controlled|export))\b",
    # Defense-adjacent employers / contracts (most cleared-only jobs live here).
    r"(?i)\b(dod|department\s+of\s+defense|federal\s+(?:contract|contractor|government|client)|government\s+clearance|national\s+security)\b",
]


# ---------------------------------------------------------------------------
# Seniority ladder. Lower rank = more junior.
#
# Word boundaries on every regex are deliberate: they keep "Seniority"
# from matching the senior tier, "Staffing" from matching the staff
# tier, "Lead Generation" is intentional behaviour (handled below).
# ---------------------------------------------------------------------------
SENIORITY_TIERS: list[tuple[str, int, str]] = [
    ("intern",    0, r"\b(?:intern(?:ship)?|co[\s-]?op)\b"),
    ("junior",    1, r"\b(?:junior|jr\.?|associate|entry[\s-]?level|new[\s-]?grad|graduate)\b"),
    ("mid",       2, r"\b(?:mid[\s-]?level|intermediate)\b"),
    ("senior",    3, r"\b(?:senior|sr\.?|level[\s-]?3|iii|l3|e[34]|ic3|p3|engineer[\s-]?ii)\b"),
    ("staff",     4, r"\b(?:staff|level[\s-]?4|iv|l4|e[56]|ic4|p4)\b"),
    # ``staff+``/``staff plus`` is a Stripe-style "above staff" alias.
    # We can't put a trailing ``\b`` on the literal ``staff+`` because
    # ``+`` is a non-word character and so is the trailing space —
    # ``\b`` only fires between a word and a non-word character, so a
    # trailing ``\b`` here would never match. The literal ``+`` itself
    # is the disambiguator (no other tier spells ``staff+``), so a
    # bare alternative outside the \b group is correct.
    ("principal", 5, r"\b(?:principal|distinguished|level[\s-]?5|l5|e[67]|ic5|p5)\b|staff\+"),
    # Lead is intentionally a *qualified* match: tech lead / team lead /
    # engineering lead / group lead. A bare "Lead" at the start of a
    # title (e.g. "Lead Generation Specialist") matches \blead\b but
    # that title is also surfaced by the positive-relevant-match check
    # in is_relevant_role, which fails on it — so a "Lead Generation
    # Specialist" still gets dropped. We require a qualifier to keep
    # the band-check unambiguous; the un-qualified "Lead Engineer" can
    # ask the operator to clarify if they want it.
    ("lead",      5, r"\b(?:team\s+lead|tech\s+lead|engineering\s+lead|group\s+lead)\b"),
    ("manager",   6, r"\b(?:engineering\s+manager|eng\.?\s*(?:mgr|manager)|manager)\b"),
    ("director",  7, r"\b(?:director|senior\s+director|fellow|head\s+of)\b"),
    ("vp",        8, r"\b(?:vp|vice\s+president|svp|senior\s+vice\s+president|chief)\b"),
]

# Public rank lookup and value tuple — used by the Preferences singleton
# to widen its Literal in lockstep. :func:`seniority_rank` returns -1
# for ``None``/unknown.
SENIORITY_VALUES: tuple[str, ...] = tuple(name for name, _, _ in SENIORITY_TIERS)
SENIORITY_RANKS: dict[str, int] = {name: rank for name, rank, _ in SENIORITY_TIERS}

# Implicit rank assigned to titles that don't classify into any tier
# when the operator opted in to a band (sensible mid default — see
# module docstring).
IMPLICIT_MID_RANK = 2


# Typed alias — the Preferences singleton consumes this ``Literal``,
# which widens in lockstep with :data:`SENIORITY_TIERS`. Python 3.12+
# accepts the dynamic unpacking form (the project's
# ``pyproject.toml`` declares ``requires-python = ">=3.12"``).
SeniorityTier = Literal[*SENIORITY_VALUES]  # type: ignore[valid-type]


def classify_seniority(title: str) -> Optional[str]:
    """Return the highest-rank tier whose regex matches the title.

    Ties (e.g. "Senior Staff Engineer" matches both ``senior`` and
    ``staff``): the higher rank wins so the operator's bound treats
    ambiguous titles as the more senior cohort — better to over-include
    than silently drop "Senior Staff" when ``min_seniority=staff``.

    Returns ``None`` for unclassifiable titles ("Software Engineer"
    alone has no seniority hint). :func:`is_relevant_role` reads that
    as :data:`IMPLICIT_MID_RANK` under band-filter mode.
    """
    if not title:
        return None
    lowered = title.lower()
    best_name: Optional[str] = None
    best_rank: int = -1
    for name, rank, pattern in SENIORITY_TIERS:
        if re.search(pattern, lowered):
            if rank > best_rank:
                best_rank = rank
                best_name = name
    return best_name


def seniority_rank(name: Optional[str]) -> int:
    """Map a tier name → its rank. ``-1`` for ``None``/unknown.

    Stable across module reloads because the lookup reads
    :data:`SENIORITY_RANKS` (built once at import time from
    :data:`SENIORITY_TIERS`).
    """
    if name is None:
        return -1
    return SENIORITY_RANKS.get(name, -1)


def is_relevant_role(
    title: str,
    extra_patterns: Iterable[str] | None = None,
    extra_relevant_patterns: Iterable[str] | None = None,
    description: str | None = None,
    *,
    min_seniority: Optional[SeniorityTier] = None,
    max_seniority: Optional[SeniorityTier] = None,
) -> bool:
    """Decide whether a job title + description match the operator's profile.

    See module docstring for the full pipeline order. With both
    ``min_seniority`` and ``max_seniority`` ``None`` (the default),
    seniority filtering is a no-op and the legacy keyword behaviour
    is preserved bit-for-bit — so the refactor is wire-compatible
    with the existing scan / scoring endpoints.
    """
    if not title:
        return False

    lowered_title = title.lower()
    lowered_description = (description or "").lower()
    text_to_check = f"{lowered_title} {lowered_description}"

    # 1. Hard drops: visa / clearance.
    for pattern in NO_SPONSORSHIP_PATTERNS:
        if re.search(pattern, text_to_check):
            return False
    for pattern in CLEARANCE_PATTERNS:
        if re.search(pattern, text_to_check):
            return False

    # 2. Seniority band — only when the operator opted in.
    if min_seniority is not None or max_seniority is not None:
        classified = classify_seniority(lowered_title)
        rank = (
            seniority_rank(classified)
            if classified is not None
            else IMPLICIT_MID_RANK
        )
        if rank < 0:
            # Defensive: shouldn't happen with IMPLICIT_MID_RANK above,
            # but a -1 here means "explicitly unknown" — exclude.
            return False
        if min_seniority is not None and rank < seniority_rank(min_seniority):
            return False
        if max_seniority is not None and rank > seniority_rank(max_seniority):
            return False

    # 3. Positive relevant match — wins outright.
    relevant_patterns = list(extra_relevant_patterns or []) + DEFAULT_RELEVANT_PATTERNS
    if any(re.search(pattern, lowered_title) for pattern in relevant_patterns):
        return True

    # 4. Negative irrelevant match.
    for pattern in list(extra_patterns or []) + DEFAULT_IRRELEVANT_PATTERNS:
        if re.search(pattern, lowered_title):
            return False

    # 5. Default reject.
    return False


def filter_roles(
    jobs: list[dict],
    extra_patterns: Iterable[str] | None = None,
    extra_relevant_patterns: Iterable[str] | None = None,
    *,
    min_seniority: Optional[SeniorityTier] = None,
    max_seniority: Optional[SeniorityTier] = None,
) -> list[dict]:
    """Filter a list of job dicts down to the relevant ones.

    Keyword-only ``min_seniority`` / ``max_seniority`` thread the
    band through every per-job :func:`is_relevant_role` call. The
    legacy positional callers keep working unchanged — the new
    arguments are keyword-only on both functions.
    """
    return [
        job
        for job in jobs
        if is_relevant_role(
            job.get("title", ""),
            extra_patterns=extra_patterns,
            extra_relevant_patterns=extra_relevant_patterns,
            description=job.get("description") or job.get("content") or "",
            min_seniority=min_seniority,
            max_seniority=max_seniority,
        )
    ]
