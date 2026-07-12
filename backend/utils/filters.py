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
* :data:`SENIORITY_TIERS` (new) — ladder from ``intern`` -> ``vp``` with
  per-tier regex. The relevant range is bounded at runtime by the
  ``min_seniority`` / ``max_seniority`` knobs that come from the
  Preferences singleton (see :mod:`routes.settings`).

Seniority classification algorithm
-----------------------------------

:class:`classify_seniority` returns the **highest-rank** tier whose
regex matches the title. "Senior Staff Engineer" matches both "senior"
(rank 3) and "staff" (rank 4), returning ``"staff"``.. "Senior Director"
matches both "senior" and "director" — returns ``"director"``. "Lead"
sits at rank 5 (peers with ``principal``) because IC-leadership and
principal are interchangeable in many leveling rubrics; the operator's
band shouldn't surprise on that boundary.

Implicit mid-rank for unclassifiable titles
------------------------------------------

Titles like ``"Software Engineer"`` (no seniority hint) do not match
any tier in the ladder. The :func:`is_relevant_role` band check treats
those titles as **rank 2** (mid). The reasoning: the operator opted in
to a band, so we default to permissive behaviour — a title without
seniority markup is usually *at least* mid-level (rarely entry-level).
With ``min=mid, max=staff`` an unmarked "Software Engineer" passes;
with ``min=senior`` it fails. Sensible defaults and keeps the operator
in control.

Title-reject gate (separate from :func:`is_relevant_role`)
-----------------------------------------------------------

:func:`should_reject_by_title` is a STAND-ALONE gate that the
boards runner invokes at the per-job loop entry point. It is
NOT inside :func:`is_relevant_role` -- it runs first and
short-circuits on title-only keyword matches for the
``{staff, principal, lead, head, director}`` canonical set.

Band-knob override (read this before changing max_seniority)
------------------------------------------------------------

Because the title-reject gate runs BEFORE the band filter, it
**overrides** the operator's ``max_seniority`` setting for the
five canonical tokens. Concretely: even when an operator sets
``max_seniority="staff"`` (which would normally let ``"Staff
Engineer"`` pass the band check), the title-reject gate still
drops it. An operator who wants ``"Staff Engineer"`` roles
visible must narrow ``BOARDS_REJECT_TITLE_KEYWORDS`` (e.g.
``BOARDS_REJECT_TITLE_KEYWORDS="principal,lead,head,director"``).
The module-level ``_DEFAULT_TITLE_REJECT_KEYWORDS`` tuple is
the source of truth for the env-fallback path.

Pipeline order in the boards runner
-----------------------------------

1. **Title reject** -- :func:`should_reject_by_title`. First
   gate; drops on title-only keyword match.
2. **Years floor** -- :func:`min_years_required` >= 6+ drop.
3. **Citizenship / clearance org bench** -- ``bench_org_from_text``
   + :data:`CLEARANCE_PATTERNS`. Benches the whole org.
4. **Band + relevant-keyword pipeline** -- :func:`is_relevant_role`
   (see its ordering docstring below).

Within :func:`is_relevant_role`
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

import functools
import os
import re
from typing import Iterable, Literal, Optional

# ---- Years-of-experience parser ----------------------------------
# Match the common phrasings in modern postings:
#   "5+ years of experience"
#   "minimum 5 years experience"
#   "at least 5 years' experience"
#   "5 years experience required"
#   "5+ yrs experience"
# Single-digit minimum (1-9); anything >=10 is captured but the
# ``board_runner`` use case only cares about >=6, so the upper bound
# is whatever the job description says.
# The leading word boundary guards against false positives like
# "5+ excellent engineers" where 5 is just a count.
YEARS_OF_EXPERIENCE_PATTERN = re.compile(
    r"(?i)\b(?:minimum|min\.?|at\s+least|requires?|with|over|more\s+than|\+)?\s*"
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?|yr\.?)\s*(?:of\s+)?(?:experience|exp\.?)?\b"
)

# Same set as NO_SPONSORSHIP_PATTERNS but tightened to *only* the
# "required" phrasings. A loose mention ("sponsorship not available")
# rejects the role but doesn't bench the whole org; a hard requirement
# ("US citizenship required") does bench the org, because every role
# at a citizenship-locked shop will be wasted tokens for the LLM.
HARD_BENCH_TRIGGER_PATTERNS: list[str] = [
    # Citizenship / permanent-residency (REQUIRED, not just "preferred").
    r"(?i)\b(citizenship\s+required|must\s+be\s+a\s+(?:us|u\.s\.?)\s+citizen|"
    r"u\.?s\.?\s+citizenship\s+required|permanent\s+resident\s+required|"
    r"green\s+card\s+required|green\s+card\s+holder\s+required)\b",
    # Visa-block phrasings with strong "required" modifiers.
    r"(?i)\b(?:must\s+(?:have|possess)\s+(?:us|u\.s\.?)?\s*citizenship|"
    r"must\s+be\s+(?:a\s+)?(?:us|u\.s\.?)\s+citizen|cannot\s+sponsor|"
    r"will\s+not\s+sponsor|no\s+sponsorship\s+will\s+be\s+(?:provided|available)|"
    r"no\s+visa\s+sponsorship\s+available)\b",
    # v0.7 additions — contractions and concatenated citizenship phrases
    # that ATS copy uses interchangeably with the formal patterns above.
    # Same bench-the-org semantic: a single match in any posting from
    # the company removes the entire org from the hourly queue.
    #
    # Contractions of "cannot / will not sponsor". ``can'?t`` /
    # ``won'?t`` accept the apostrophe OR its omission so ATS
    # paste-strip artefacts ("cant sponsor") still match. Same
    # scope rationale as the existing "cannot sponsor" /
    # "will not sponsor" alternation — a single company-wide
    # prohibition is implied rather than a per-role choice.
    r"(?i)\b(?:can'?t|won'?t)\s+(?:provide\s+)?sponsor(?:ship)?\b",
    # Concatenated "US citizens + green card holders only".
    # Plural-aware (``citizens?`` / ``holders?``) covers both
    # singular and array-form copy. Specific enough to keep
    # "Citizens Bank is hiring" / "Citizens of country X welcome"
    # out of the match set via the ``US`` prefix + ``only`` anchor.
    r"(?i)\b(?:us|u\.s\.?)\s+citizens?\s+(?:and|or)\s+green\s+card\s+holders?\s+only\b",
    # "Official policy (is) not to sponsor" — corporate-policy
    # phrasing that implies org-wide scope rather than a per-role note.
    r"(?i)\bofficial\s+policy\s+(?:is\s+)?not\s+to\s+sponsor\b",
]


def bench_org_from_text(text: str) -> bool:
    """True when ``text`` mentions citizenship-required / hard sponsorship-
    block phrasings. Used by :func:`pipeline.nodes.jobs_boards.runner` to
    bench the whole org (not just drop the role) when ANY job at the
    company surfaces a hard requirement.

    Distinct from :data:`NO_SPONSORSHIP_PATTERNS` which is a broader
    role-drop gate — a "sponsorship not available" mention drops the
    role but doesn't necessarily mean every role at the org has the
    same constraint. ``HARD_BENCH_TRIGGER_PATTERNS`` is restricted to
    the strict "required" phrasings where benching the org is the
    right call.

    The clearance patterns already in :data:`CLEARANCE_PATTERNS` are
    implicitly "hard bench" (no DoD / federal contractor is going to
    sponsor anyone who can't clear) so this helper does NOT include
    them — boards runner checks clearance via the existing role-drop
    path and adds the org to the bench list when that fires.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(pat, lowered) for pat in HARD_BENCH_TRIGGER_PATTERNS)


def min_years_required(text: str) -> int | None:
    """Pull the minimum years-of-experience number from a job description.

    Returns the lowest positive integer where the LLM-style hint appears.
    Used by the boards runner to drop roles that hard-require >=6 years
    (the user wants those discarded before the LLM sees them). Returns
    ``None`` when no minimum-years pattern matches — the absence of a
    number is interpreted as "no explicit floor" and the role PROCEEDS.

    Caveats (documented so a future review doesn't ask "why did X pass?"):

    * Only the FIRST match is returned. A posting stating "3 years +
      5 years of senior X experience" picks 3, which is the more
      permissive interpretation. If we ever need the strictest floor
      we can return ``max()`` across matches; for the v1 use case
      "discard if 6+" the first match suffices.
    * Phrases like "5+ years" return 5. The runner treats "5" as
      "minimum 5", so 5+ is inclusive of 5 itself.
    * Returns ``None`` for senior-level roles like "Staff Engineer"
      when no years number is stated. Those pass the years filter;
      the seniority band filter (Preferences) governs their
      fate instead.
    """
    if not text:
        return None
    lowered = text.lower()
    # Just the leading aggregate so re.search picks the first hit.
    match = YEARS_OF_EXPERIENCE_PATTERN.search(lowered)
    if not match:
        return None
    try:
        n = int(match.group(1))
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    return n


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
    # v0.7 additions. Looser contract than HARD_BENCH_TRIGGER_PATTERNS:
    # a match here DROPS THE ROLE but does NOT bench the whole org,
    # because a hyphenated/contracted mention is often per-role copy
    # rather than company-wide policy. Operator can flip a wrongly
    # dropped row back to "approved" via the JobBoard status dropdown.
    #
    # Hyphen-tolerant "no sponsorship" / "no visa support".
    r"(?i)\bno[\s-](?:sponsorship|visa[\s-]support)\b",
    # Hyphenated + contracted cannot-sponsor / will-not-sponsor /
    # can't / won't sponsor. Apostrophe optional ("cant sponsor"
    # ATS paste-strip artefact still matches).
    r"(?i)\b(?:can\'?t|won\'?t|cannot|will[\s-]+not)[\s-]sponsor\b",
    # Sponsorship alternates: "sponsorship unavailable" /
    # "sponsorship (is) not offered / not provided".
    r"(?i)\bsponsorship\s+(?:is\s+)?(?:unavailable|not\s+(?:offered|provided))\b",
    # Eligibility negations: "not eligible to sponsor" /
    # "isn't / aren't eligible for sponsorship".
    r"(?i)\b(?:isn\'?t|aren\'?t|not)\s+eligible\s+(?:to|for)\s+sponsor(?:ship)?\b",
    # "not open to sponsorship".
    r"(?i)\bnot\s+open\s+to\s+sponsorship\b",
    # UK + US work-authorisation phrasings. Geographic context
    # REQUIRED so "right to work from home" does NOT false-positive.
    r"(?i)\b(?:must\s+be\s+(?:legally\s+)?)?(?:authorised|authorized)\s+to\s+work\s+in\s+(?:the\s+)?(?:uk|u\.k\.?|united\s+kingdom|us|u\.s\.?|united\s+states)\b",
    # H-1B alternates ("not sponsored", "not offered") alongside the
    # already-covered "not provided".
    r"(?i)\bh-?1b\s+(?:not\s+sponsored|not\s+offered)\b",
    # "US citizens only" / "US citizen only" (narrowed with US prefix
    # + "only" anchor so "Senior Citizens Academy" / "Citizens Bank"
    # stay out of the match set).
    r"(?i)\b(?:us|u\.s\.?)\s+citizens?\s+only\b",
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
    # ``+`` is a non-word character and so is the trailing space --
    # ``\b`` only fires between a word and a non-word character, so a
    # trailing ``\b`` here would never match. The literal ``+`` itself
    # is the disambiguator (no other tier spells ``staff+``), so a
    # bare alternative outside the \b group is correct.
    ("principal", 5, r"\b(?:principal|distinguished|level[\s-]?5|l5|e[67]|ic5|p5)\b|staff\+"),
    # Lead is intentionally a *qualified* match: tech lead / team lead /
    # engineering lead / group lead. A bare "Lead" at the start of a
    # title (e.g. "Lead Generation Specialist") matches \blead\b but
    # that title is also surfaced by the positive-relevant-match check
    # in is_relevant_role, which fails on it -- so a "Lead Generation
    # Specialist" still gets dropped. We require a qualifier to keep
    # the band-check unambiguous; the un-qualified "Lead Engineer" can
    # ask the operator to clarify if they want it.
    ("lead",      5, r"\b(?:team\s+lead|tech\s+lead|engineering\s+lead|group\s+lead)\b"),
    ("manager",   6, r"\b(?:engineering\s+manager|eng\.?\s*(?:mgr|manager)|manager)\b"),
    ("director",  7, r"\b(?:director|senior\s+director|fellow|head\s+of)\b"),
    ("vp",        8, r"\b(?:vp|vice\s+president|svp|senior\s+vice\s+president|chief)\b"),
]

# Public rank lookup and value tuple -- used by the Preferences singleton
# to widen its Literal in lockstep. :func:`seniority_rank` returns -1
# for ``None``/unknown.
SENIORITY_VALUES: tuple[str, ...] = tuple(name for name, _, _ in SENIORITY_TIERS)
SENIORITY_RANKS: dict[str, int] = {name: rank for name, rank, _ in SENIORITY_TIERS}

# Implicit rank assigned to titles that don't classify into any tier
# when the operator opted in to a band (sensible mid default -- see
# module docstring).
IMPLICIT_MID_RANK = 2


# Typed alias -- the Preferences singleton consumes this ``Literal``,
# which widens in lockstep with :data:`SENIORITY_TIERS`. Python 3.12+
# accepts the dynamic unpacking form (the project's
# ``pyproject.toml`` declares ``requires-python = ">=3.12"``).
SeniorityTier = Literal[*SENIORITY_VALUES]  # type: ignore[valid-type]


def classify_seniority(title: str) -> Optional[str]:
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
    """Map a tier name -> its rank. ``-1`` for ``None``/unknown.

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
    is preserved bit-for-bit -- so the refactor is wire-compatible
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

    # 2. Seniority band -- only when the operator opted in.
    if min_seniority is not None or max_seniority is not None:
        classified = classify_seniority(lowered_title)
        rank = (
            seniority_rank(classified)
            if classified is not None
            else IMPLICIT_MID_RANK
        )
        if rank < 0:
            # Defensive: shouldn't happen with IMPLICIT_MID_RANK above,
            # but a -1 here means "explicitly unknown" -- exclude.
            return False
        if min_seniority is not None and rank < seniority_rank(min_seniority):
            return False
        if max_seniority is not None and rank > seniority_rank(max_seniority):
            return False

    # 3. Positive relevant match -- wins outright.
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
    legacy positional callers keep working unchanged -- the new
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


# ---------------------------------------------------------------------------
# Title-level reject filter.
#
# Independent of the seniority band: the operator's profile explicitly does
# not align with roles promoted by ``staff`` / ``principal`` / ``lead`` /
# ``head`` / ``director`` regardless of the band filter outcome. The
# canonical 5-token set matches the "above-staff" tier names in
# :data:`SENIORITY_TIERS` plus two common adjacent titles (``head``,
# ``director``) -- the per-experience rationale lives in the operator's
# profile rather than the band knobs.
#
# Why this gate is separate from :func:`is_relevant_role`
# --------------------------------------------------------
#
# :func:`is_relevant_role` honours ``min_seniority`` / ``max_seniority``.
# Setting ``max_seniority="staff"`` keeps ``"Staff Engineer"`` in the
# queue but drops ``"Principal Engineer"`` -- that's the *band* contract.
# The title-reject contract is sharper: the operator's profile says
# "I don't interview for staff roles at all" regardless of the band
# knobs. So the gate runs BEFORE the band check, at the boards runner's
# per-job loop entry point, and short-circuits on the title alone.
#
# Env-override path
# -----------------
#
# ``BOARDS_REJECT_TITLE_KEYWORDS`` accepts comma- OR whitespace-separated
# tokens (e.g. ``"staff,principal"`` or ``"staff principal lead"``).
# Empty / missing / whitespace-only env falls back to the canonical
# 5-token default -- a misconfigured Render secret doesn't silently
# disable the gate. Tokens are deduplicated and lowercased so an
# operator who exports ``"STAFF,Staff"`` gets the same set as
# ``"staff"``.
#
# Word-boundary semantics
# -----------------------
#
# ``\b`` keeps ``Staffing`` / ``Leadership`` / ``Directorship`` out of
# the match set: those titles aren't seniority prompts, they're
# department / role-family names. The downside is that a "Lead
# Generation Marketer" title *does* hit "lead" -- but a Lead
# Generation Marketer doesn't match the positive-relevant keyword
# bucket either, so it would be dropped downstream by
# :func:`is_relevant_role` anyway; saving the LLM tokens is a net win.
# ---------------------------------------------------------------------------
_DEFAULT_TITLE_REJECT_KEYWORDS: tuple[str, ...] = (
    "staff",
    "principal",
    "lead",
    "head",
    "director",
)


def _resolve_title_reject_keywords(
    raw_env: Optional[str] = None,
) -> tuple[str, ...]:
    """Resolve the effective keyword set from BOARDS_REJECT_TITLE_KEYWORDS.

    Accepts comma- OR whitespace-separated tokens. Empty / unset /
    whitespace-only env returns the canonical default so a missing
    or whitespace-only secret is non-destructive (the gate stays
    enabled).

    ``raw_env`` is exposed for tests so the suite can simulate
    ``os.environ`` mutations without polluting the real env.
    """
    raw = raw_env if raw_env is not None else os.environ.get(
        "BOARDS_REJECT_TITLE_KEYWORDS", ""
    )
    if not raw:
        return _DEFAULT_TITLE_REJECT_KEYWORDS
    tokens: list[str] = []
    for piece in re.split(r"[,\s]+", raw):
        piece = piece.strip().lower()
        if piece and piece not in tokens:
            tokens.append(piece)
    if not tokens:
        return _DEFAULT_TITLE_REJECT_KEYWORDS
    return tuple(tokens)


@functools.lru_cache(maxsize=128)
def _build_title_reject_pattern(
    keywords_tuple: Optional[tuple[str, ...]],
    raw_env: Optional[str],
) -> "re.Pattern[str]":
    """Compile the title-reject regex, cached on (keywords, env).

    Cache key is ``(keywords_tuple, raw_env)``: identical inputs
    return the SAME compiled pattern across calls. The production
    path (no explicit ``keywords=``, ``raw_env=None``) hits ONE
    cache slot per env-rotation -- the boards runner's hot loop
    pays a single ``re.compile`` cost for the whole scan instead
    of two-thousand.

    Test path varies ``raw_env=`` per case to exercise both
    cached-reuse and cache-miss paths without polluting
    ``os.environ``. ``maxsize=128`` is generous (a 200-job scan
    might see 8-12 distinct env values from GHA's pre-deploy
    steps; tests routinely exceed that during parametrized runs).

    Empty / all-whitespace keyword tuple returns a never-match
    sentinel (``(?!)``) so callers don't have to special-case
    empty input -- ``pattern.search("anything")`` returns
    ``None`` cleanly.
    """
    if keywords_tuple is not None:
        effective = keywords_tuple
    else:
        effective = _resolve_title_reject_keywords(raw_env=raw_env)
    if not effective:
        # Never-match sentinel: (?!) is a negative lookahead with
        # an empty assertion -- it always fails, so .search() on
        # any input returns None. Alternative ``\b\B`` would also
        # work but is less readable.
        return re.compile(r"(?!)")
    return re.compile(
        r"(?i)\b(?:" + "|".join(re.escape(k) for k in effective) + r")\b"
    )


def should_reject_by_title(
    title: str,
    *,
    keywords: Optional[Iterable[str]] = None,
    raw_env: Optional[str] = None,
) -> bool:
    """True when ``title`` contains any of the seniority-promotion keywords.

    Implementation note
    -------------------
    The regex is COMPILED ONCE per ``(keyword-set, env-value)`` pair
    via :func:`_build_title_reject_pattern`'s ``lru_cache``. The
    production path hits one cache slot per env-rotation -- the
    boards runner scans 200-2000 jobs per tick and we don't want a
    per-job ``re.compile``. Tests vary ``raw_env=`` per case to
    exercise cache-hit, cache-miss, and the env-fallback path
    deterministically.

    Parameters
    ----------
    title
        Job title text. Empty / falsy returns ``False`` (default
        allow) so the upstream band filter owns the call.
    keywords
        Optional explicit keyword list. When ``None`` (the
        default), reads ``BOARDS_REJECT_TITLE_KEYWORDS`` via
        :func:`_resolve_title_reject_keywords` and falls back to
        the canonical 5-token set on unset / empty env. Useful
        for unit tests that want to lock the keyword set without
        mutating ``os.environ``.
    raw_env
        Test hook -- pass the env-var value directly without
        touching ``os.environ``. In production this stays
        ``None``. Takes precedence over the live env when set.

    Returns
    -------
    bool
        ``True`` when the title matches at least one keyword as a
        case-insensitive whole word; ``False`` otherwise (including
        empty title / empty keyword list -- both are no-ops).

    Match semantics
    ---------------
    Word-boundary matches are case-insensitive. ``"Staff Engineer"``
    and ``"staff engineer"`` both return ``True``; ``"Staffing
    Coordinator"`` and ``"Leadership Program"`` return ``False``.

    Why this gate is separate from :func:`is_relevant_role`
    -------------------------------------------------------
    Setting ``max_seniority="staff"`` keeps ``"Staff Engineer"``
    in the queue (band contract). The title-reject contract is
    sharper: the operator's profile says "I don't interview
    for staff roles at all" regardless of the band knobs -- so
    the gate runs BEFORE :func:`is_relevant_role` at the boards
    runner's per-job loop entry point and short-circuits on the
    title alone. Concretely: even if an operator sets
    ``max_seniority="staff"``, this gate still drops
    ``"Staff Engineer"`` -- the band knob does NOT override the
    title-reject gate.

    Title-only by design
    --------------------
    Unlike :func:`min_years_required` which scans the COMBINED
    ``"{title} {description}"`` text, this gate inspects the
    TITLE ONLY. The rationale: a description-side mention of
    "staff-level" is ambiguous (could be the role's level, could
    be a disambiguating phrase). The operator's intent is "if the
    TITLE says staff, drop it".
    """
    if not title:
        return False
    if keywords is not None:
        kw_tuple = tuple(
            piece.strip().lower()
            for piece in keywords
            if piece and piece.strip()
        )
    else:
        kw_tuple = None
    pattern = _build_title_reject_pattern(kw_tuple, raw_env)
    return bool(pattern.search(title))
