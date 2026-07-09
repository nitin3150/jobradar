"""Profile service — YAML-backed candidate profile loader/saver for the LLM scorer.

The profile is the **single source of truth** for the candidate's career
context. It lives at ``config/profile.yml`` (gitignored, like .env) and
falls back to ``config/profile.example.yml`` when the operator hasn't
created their own yet. The LLM scorer reads this profile (NOT the
Q&A bank) to evaluate job fit.

Wire shape mirrors the YAML structure documented in
``config/profile.example.yml``:

* ``candidate`` — name, email, phone, location, social links
* ``target_roles.primary`` — flat list of role titles the operator optimizes for
* ``target_roles.archetypes`` — list of ``{name, level, fit: primary|secondary|adjacent}``
* ``narrative`` — headline, exit_story, superpowers, proof_points
* ``compensation`` — target_range, currency, minimum, location_flexibility
* ``location`` — country, city, timezone, visa_status

The LLM scoring prompt (:func:`build_profile_summary`) and the boards
runner's initial filter (:func:`get_all_target_roles`,
:func:`get_target_roles_by_fit`) both read from this profile. The
Q&A bank is reserved for the application form auto-fill (out of
scope here) — the scorer no longer reads it.

YAML handling note
==================

PyYAML is required to parse the YAML file. It is added to
``backend/pyproject.toml`` ``[project] dependencies`` as part of
this PR; without it the import fails fast with a clear
``ModuleNotFoundError`` so the operator sees a single actionable
error rather than a confusing ``NameError: yaml`` deeper in the
call stack.

Gitignore
=========

``config/profile.yml`` contains the operator's PII (name, email,
phone, location, compensation, target roles). It is added to
``.gitignore`` next to the existing ``.env`` line. The example
file (``config/profile.example.yml``) IS committed because it
serves as both the template and the fallback when no operator
profile exists.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

# ``LLMClient`` is imported at module scope (not lazily inside
# :func:`extract_profile_from_resume`) so tests can monkeypatch
# ``services.profile_service.LLMClient.from_env`` to mock the LLM
# call. The lazy-import pattern would have made the patch target
# unreachable from the test (the name wouldn't exist as a module
# attribute). The runtime cost of the extra import is negligible
# — ``services.llm_client`` is already imported by the boards-scan
# path on any deployment that has NVIDIA_API_KEY or GROQ_API_KEY set.
from services.llm_client import LLMClient


# Repo root: backend/services/profile_service.py → backend/services/ → backend/ → REPO_ROOT.
# Parents[0]=services, [1]=backend, [2]=repo root.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
CONFIG_DIR: Path = REPO_ROOT / "config"
PROFILE_PATH: Path = CONFIG_DIR / "profile.yml"
EXAMPLE_PATH: Path = CONFIG_DIR / "profile.example.yml"


# In-process cache so the LLM scorer doesn't re-read+re-parse the
# YAML on every opportunity. ``save_profile`` invalidates the cache
# so the next ``load_profile`` picks up the on-disk state. The cache
# is process-local; two ``LLMClient`` instances in the same FastAPI
# process share it via this module-level singleton.
_cached_profile: Optional["Profile"] = None


# ----------------------------------------------------------------------
# Pydantic models
# ----------------------------------------------------------------------


class Candidate(BaseModel):
    """PII block — name, contact info, social links.

    Every field is optional because the LLM extraction on a resume
    is best-effort: a resume without a phone number still validates,
    and ``build_profile_summary`` gracefully omits empty fields from
    the rendered prompt.
    """

    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None
    portfolio_url: Optional[str] = None
    github: Optional[str] = None
    twitter: Optional[str] = None


# ``fit`` is the load-bearing tag for the boards runner weighting:
# primary = dream role (always pass the prefilter), secondary =
# good fit (pass with normal LLM scoring), adjacent = stretch
# (pass with reduced weight in the heuristic prefilter so a sea
# of adjacent matches doesn't drown the in-review queue). The
# Literal here mirrors the YAML comment in profile.example.yml.
FitLevel = Literal["primary", "secondary", "adjacent"]


class Archetype(BaseModel):
    """A role archetype with a fit-level tag.

    ``level`` is free-text (the example uses ``"Senior/Staff"``,
    ``"Mid-Senior"``) so the schema stays flexible — the operator
    can write whatever seniority phrasing matches the job market
    they're applying to without a schema migration.
    """

    name: str
    level: Optional[str] = None
    fit: FitLevel = "primary"


class TargetRoles(BaseModel):
    """The candidate's target role list.

    ``primary`` is a flat list of role titles. ``archetypes`` is a
    richer list of role families with fit-level tagging. The
    boards runner and the LLM scoring prompt both read the union
    via :func:`get_all_target_roles` and
    :func:`get_target_roles_by_fit`.
    """

    primary: list[str] = Field(default_factory=list)
    archetypes: list[Archetype] = Field(default_factory=list)


class ProofPoint(BaseModel):
    """A concrete proof of impact (project, article, case study).

    ``name`` is required because the rendered prompt lists
    "Proof points: - Project Alpha" — an entry without a name
    would render as ``- None (Reduced inference latency 40%)``
    which is nonsense.
    """

    name: str
    url: Optional[str] = None
    hero_metric: Optional[str] = None


class Narrative(BaseModel):
    """The candidate's professional story."""

    headline: Optional[str] = None
    exit_story: Optional[str] = None
    superpowers: list[str] = Field(default_factory=list)
    proof_points: list[ProofPoint] = Field(default_factory=list)


class Compensation(BaseModel):
    """Compensation expectations.

    Kept as free-text strings (``"$150K-200K"``, ``"$120K"``) rather
    than numeric ranges because the operator writes it in their
    preferred format. The LLM scoring prompt only needs the text;
    the field is purely informational, not a filter.
    """

    target_range: Optional[str] = None
    currency: Optional[str] = None
    minimum: Optional[str] = None
    location_flexibility: Optional[str] = None


class Location(BaseModel):
    """Where the candidate is / where they'll work.

    ``visa_status`` drives a future filter (we don't auto-filter
    today — a posting that hard-requires sponsorship is benched
    by the boards runner's clearance gate, not by the profile).
    """

    country: Optional[str] = None
    city: Optional[str] = None
    timezone: Optional[str] = None
    visa_status: Optional[str] = None


class Profile(BaseModel):
    """Top-level profile model.

    All sub-blocks default to empty so a freshly-extracted profile
    (or a partially-filled hand edit) passes validation. The LLM
    scorer renders an empty profile as ``"(no profile configured)"``
    and behaves the same as the old ``build_profile_summary`` did
    when neither ``target_roles`` nor the Q&A bank was populated.
    """

    candidate: Candidate = Field(default_factory=Candidate)
    target_roles: TargetRoles = Field(default_factory=TargetRoles)
    narrative: Narrative = Field(default_factory=Narrative)
    compensation: Compensation = Field(default_factory=Compensation)
    location: Location = Field(default_factory=Location)


# ----------------------------------------------------------------------
# Loading + saving
# ----------------------------------------------------------------------


def get_profile_path() -> Path:
    """Return the path to the active profile, falling back to the example.

    The operator's actual profile lives at ``config/profile.yml``
    (gitignored, like .env). When it doesn't exist (fresh clone,
    pre-onboarding), we fall back to ``config/profile.example.yml``
    so the LLM scorer has *something* to score against. The
    example file is always present in the repo so the fallback
    is deterministic.
    """
    if PROFILE_PATH.is_file():
        return PROFILE_PATH
    return EXAMPLE_PATH


def load_profile(
    *,
    use_cache: bool = True,
    path: Optional[Path] = None,
) -> Profile:
    """Load the profile from disk, falling back to the example if missing.

    Args:
        use_cache: When True (default), return the in-memory cached
            profile if one exists. ``save_profile`` invalidates the
            cache so the next load picks up the on-disk state.
            Tests pass ``use_cache=False`` to force a fresh read
            from a tempdir.
        path: Override the default path. Tests use this to point
            at a tempdir fixture; production never passes it.

    Returns:
        A validated :class:`Profile`. An empty :class:`Profile()` is
        returned when neither ``profile.yml`` nor
        ``profile.example.yml`` exists on disk — callers should
        treat that the same as a pre-onboarding state.
    """
    global _cached_profile
    if use_cache and _cached_profile is not None:
        return _cached_profile

    target = path or get_profile_path()
    if not target.is_file():
        # No profile anywhere — return empty. The LLM scorer
        # renders this as ``(no profile configured)`` and
        # degrades gracefully.
        profile = Profile()
    else:
        with open(target, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # ``Profile(**data)`` raises pydantic.ValidationError on a
        # malformed file. The error propagates to the caller (the
        # resume upload route catches it and returns 422; the
        # scorer logs a warning and uses an empty profile).
        profile = Profile(**data)

    if use_cache:
        _cached_profile = profile
    return profile


def save_profile(
    profile: Profile,
    *,
    path: Optional[Path] = None,
) -> Path:
    """Write the profile to ``config/profile.yml`` as YAML.

    Args:
        profile: The Pydantic :class:`Profile` to serialize.
        path: Override the default save target. Mostly used by tests
            that write into a tempdir.

    Returns:
        The path that was written.

    The cache is invalidated on save so the next ``load_profile``
    call picks up the on-disk state. ``model_dump(exclude_none=True)``
    keeps the YAML file tight — null fields are omitted rather
    than rendered as ``key: null``.

    YAML formatting choices:
        * ``default_flow_style=False`` — every collection is a
          block list, not an inline ``[a, b, c]`` — matches the
          human-readable example format.
        * ``sort_keys=False`` — preserves the model field order
          (``candidate`` first, then ``target_roles``, etc.) so
          the file reads top-to-bottom in the same order as the
          Pydantic model.
        * ``allow_unicode=True`` — keeps non-ASCII names (e.g.
          ``"José"``) verbatim rather than escaping them.
    """
    global _cached_profile
    target = path or PROFILE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            profile.model_dump(exclude_none=True),
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
    _cached_profile = None
    return target


def reset_cache() -> None:
    """Test seam: clear the in-memory profile cache.

    Production code never calls this — :func:`save_profile` invalidates
    the cache automatically. Tests use this between cases so a
    fixture from one test doesn't leak into the next.
    """
    global _cached_profile
    _cached_profile = None


# ----------------------------------------------------------------------
# Target roles
# ----------------------------------------------------------------------


def get_all_target_roles(profile: Optional[Profile] = None) -> list[str]:
    """Flat list of all role names from primary + archetypes.

    Used by the boards runner's initial filter and the
    ``boards_scan.py`` CLI when ``TARGET_ROLES`` env is unset.
    Order: primary first (in declaration order), then archetypes
    in declaration order with duplicates dropped (so a role that
    appears in both ``primary`` and ``archetypes`` is only listed
    once).
    """
    if profile is None:
        profile = load_profile()
    roles: list[str] = list(profile.target_roles.primary)
    for arch in profile.target_roles.archetypes:
        if arch.name not in roles:
            roles.append(arch.name)
    return roles


def get_target_roles_by_fit(
    profile: Optional[Profile] = None,
) -> dict[FitLevel, list[str]]:
    """Roles grouped by fit level (primary/secondary/adjacent).

    Drives the boards runner weighting: primary roles always
    pass the heuristic prefilter, secondary roles pass with
    normal LLM scoring, adjacent roles get a reduced weight so
    a sea of adjacent matches doesn't drown the in-review queue.
    The weights themselves are a Step-4 decision (the runner
    currently uses a flat role-match without weighting); this
    function provides the input shape.

    Archetypes with ``fit="primary"`` are merged into the primary
    bucket alongside ``target_roles.primary``. The example profile
    has both (``"Senior AI Engineer"`` in primary, and
    ``"AI/ML Engineer"`` archetype with ``fit="primary"``), so a
    naive implementation that only iterates ``secondary`` and
    ``adjacent`` archetypes would silently drop the ``"primary"``
    archetype names — they wouldn't appear in any bucket and the
    boards runner's Step-4 weighting would under-represent them.
    The ``arch.name not in primary_seen`` guard dedupes against
    ``target_roles.primary`` so a role listed in both places
    appears once.
    """
    if profile is None:
        profile = load_profile()
    primary_seen = set(profile.target_roles.primary)
    primary_bucket: list[str] = list(profile.target_roles.primary)
    secondary_bucket: list[str] = []
    adjacent_bucket: list[str] = []
    for arch in profile.target_roles.archetypes:
        if arch.fit == "primary":
            if arch.name not in primary_seen:
                primary_seen.add(arch.name)
                primary_bucket.append(arch.name)
        elif arch.fit == "secondary":
            secondary_bucket.append(arch.name)
        elif arch.fit == "adjacent":
            adjacent_bucket.append(arch.name)
    return {
        "primary": primary_bucket,
        "secondary": secondary_bucket,
        "adjacent": adjacent_bucket,
    }


# ----------------------------------------------------------------------
# Profile summary (LLM scoring prompt)
# ----------------------------------------------------------------------


def build_profile_summary(profile: Optional[Profile] = None) -> str:
    """Render the profile as a markdown block for the LLM scoring prompt.

    The output replaces the old ``build_profile_summary`` from
    :mod:`services.scoring_service` that mixed ``target_roles`` from
    ``_PREFS_STATE`` with Q&A bank entries. Per the user's request,
    the Q&A bank is no longer used for scoring — only the rich
    profile feeds the LLM. The Q&A bank remains for application
    form auto-fill (out of scope here).

    Returns:
        A multi-line string suitable for splicing into
        :func:`services.llm_client.build_prompt`. An empty profile
        renders as ``"(no profile configured)"`` so the LLM still
        gets a coherent (if minimal) candidate context — the
        scoring behaviour is then identical to the pre-profile
        era when no Q&A bank was populated.
    """
    if profile is None:
        profile = load_profile()

    parts: list[str] = []

    # Target roles — the most important section. The fit-level
    # grouping teaches the LLM the priority order so a primary
    # match scores above a secondary match above an adjacent
    # match, all else equal.
    by_fit = get_target_roles_by_fit(profile)
    if any(by_fit.values()):
        lines = ["Target roles:"]
        if by_fit["primary"]:
            lines.append("Primary (dream roles):")
            lines.extend(f"  - {r}" for r in by_fit["primary"])
        if by_fit["secondary"]:
            lines.append("Secondary (good fit):")
            lines.extend(f"  - {r}" for r in by_fit["secondary"])
        if by_fit["adjacent"]:
            lines.append("Adjacent (stretch):")
            lines.extend(f"  - {r}" for r in by_fit["adjacent"])
        parts.append("\n".join(lines))

    # Narrative — the candidate's story. The LLM uses this to
    # decide whether a job's responsibilities match the operator's
    # actual day-to-day, not just the title.
    if profile.narrative.headline:
        parts.append(f"Headline: {profile.narrative.headline}")
    if profile.narrative.exit_story:
        parts.append(f"Exit story: {profile.narrative.exit_story}")
    if profile.narrative.superpowers:
        parts.append(
            "Superpowers:\n"
            + "\n".join(f"- {s}" for s in profile.narrative.superpowers)
        )
    if profile.narrative.proof_points:
        proof_lines: list[str] = []
        for pp in profile.narrative.proof_points:
            line = f"- {pp.name}"
            if pp.hero_metric:
                line += f" ({pp.hero_metric})"
            if pp.url:
                line += f": {pp.url}"
            proof_lines.append(line)
        parts.append("Proof points:\n" + "\n".join(proof_lines))

    # Candidate identity + visa status. ``visa_status`` is
    # surfaced in the prompt so the LLM can flag a posting that
    # hard-requires sponsorship (matching the boards runner's
    # clearance gate, but catching nuanced phrasings the
    # regex misses).
    cand_parts: list[str] = []
    if profile.candidate.full_name:
        cand_parts.append(profile.candidate.full_name)
    if profile.candidate.location:
        cand_parts.append(f"based in {profile.candidate.location}")
    if profile.location.visa_status:
        cand_parts.append(f"({profile.location.visa_status})")
    if cand_parts:
        parts.append("Candidate: " + ", ".join(cand_parts))

    # Compensation — the LLM uses this to score a posting whose
    # listed range falls below the operator's minimum as a
    # soft-mismatch, even if the role itself is a perfect fit.
    if profile.compensation.target_range:
        comp = f"Target comp: {profile.compensation.target_range}"
        if profile.compensation.currency:
            comp += f" {profile.compensation.currency}"
        if profile.compensation.minimum:
            comp += f" (minimum: {profile.compensation.minimum})"
        if profile.compensation.location_flexibility:
            comp += f" — {profile.compensation.location_flexibility}"
        parts.append(comp)

    # Location.
    loc_parts: list[str] = []
    if profile.location.city:
        loc_parts.append(profile.location.city)
    if profile.location.country:
        loc_parts.append(profile.location.country)
    if profile.location.timezone:
        loc_parts.append(profile.location.timezone)
    if loc_parts:
        parts.append("Location: " + ", ".join(loc_parts))

    return "\n\n".join(parts) if parts else "(no profile configured)"


# ----------------------------------------------------------------------
# Resume text extraction + LLM profile extraction
# ----------------------------------------------------------------------


_logger = logging.getLogger("jobradar.profile")


# Cap on the resume text we send to the LLM. 30 KB is roughly 7-8K
# tokens depending on whitespace + bullet density — a generous cap
# that still keeps the per-extraction LLM cost bounded. Resumes over
# this limit get TRUNCATED (not rejected) so the operator still gets
# a best-effort profile extracted from the top of their resume
# rather than a hard failure. The truncation is logged so a debug
# run can spot an over-long resume at a glance.
MAX_RESUME_CHARS = 30_000


def extract_resume_text(file_bytes: bytes, filename: str) -> str:
    """Best-effort text extraction for a stored resume.

    Supported formats:
    * ``.txt`` / ``.md`` / ``.markdown`` — decoded as UTF-8 with
      ``errors="replace"`` so a binary blob doesn't crash the
      extractor. ``replace`` swaps bad bytes for U+FFFD, which the
      LLM reads as a placeholder without confusing the JSON parser.
    * ``.pdf`` — extracted via :mod:`pypdf` if installed. We do NOT
      make ``pypdf`` a hard dependency (see :mod:`pyproject.toml` —
      it's in the runtime ``dependencies`` list but the test suite
      still imports cleanly without it because :func:`extract_resume_text`
      catches :class:`ImportError` and degrades to a UTF-8 decode
      fallback).
    * unknown extension — falls back to UTF-8 decode. Most editors
      export resumes as plain text under a misleading extension, so
      this catches the common "resume.rtf exported as .doc" case.

    Returns the extracted text (possibly empty if the file is
    truly binary). The caller is expected to log + skip on an
    empty result rather than feed empty text to the LLM (an empty
    resume produces a ``Profile()`` with no useful fields).
    """
    name = (filename or "").lower()
    if name.endswith((".txt", ".md", ".markdown")):
        return file_bytes.decode("utf-8", errors="replace")
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            _logger.warning(
                "pypdf not installed; falling back to UTF-8 decode for %s",
                filename,
            )
            return file_bytes.decode("utf-8", errors="replace")
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
        except Exception as exc:  # noqa: BLE001 — pypdf raises a wide set
            _logger.warning(
                "PDF parse failed for %s: %s; falling back to UTF-8 decode",
                filename,
                exc,
            )
            return file_bytes.decode("utf-8", errors="replace")
        # ``extract_text()`` can return ``None`` for image-only pages;
        # coalesce those to empty strings so ``"\n".join`` is safe.
        # We don't OCR — an image-only resume is out of scope for v1
        # and the operator can hand-edit profile.yml if needed.
        return "\n".join(
            (page.extract_text() or "") for page in reader.pages
        )
    # Unknown extension — try a UTF-8 decode. A binary blob returns
    # mostly U+FFFD placeholders; the LLM extraction still runs
    # (returning an empty-ish profile) rather than crashing.
    return file_bytes.decode("utf-8", errors="replace")


def _truncate_resume_text(text: str) -> str:
    """Clamp the resume to :data:`MAX_RESUME_CHARS`, logging if we cut.

    The LLM extractor accepts arbitrarily long input but the per-call
    token cost scales with length. Truncating at a 30 KB boundary
    keeps a single extraction under ~8K tokens of input, which is
    the sweet spot for a 70B-parameter model — long enough to
    capture a senior candidate's full work history + projects,
    short enough that the LLM doesn't quietly drop the proof-points
    section because it ran out of attention budget.
    """
    if len(text) > MAX_RESUME_CHARS:
        _logger.info(
            "resume text truncated from %d to %d chars (MAX_RESUME_CHARS)",
            len(text),
            MAX_RESUME_CHARS,
        )
        return text[:MAX_RESUME_CHARS]
    return text


async def extract_profile_from_resume(
    file_bytes: bytes,
    filename: str,
    *,
    llm_client: Optional["LLMClient"] = None,
) -> Profile:
    """End-to-end resume → :class:`Profile` extraction.

    Pipeline:
    1. :func:`extract_resume_text` — best-effort text extraction.
    2. :func:`_truncate_resume_text` — clamp to the LLM input budget.
    3. ``llm_client.extract_profile`` — call the LLM extractor.
    4. ``Profile(**data)`` — validate against the Pydantic model.
       Pydantic's default-tolerant behavior (every field is optional,
       every sub-block has a default_factory) means a partial dict
       validates cleanly — the LLM only needs to fill the fields
       it can identify in the resume.
    5. :func:`save_profile` — write to ``config/profile.yml`` (or the
       ``path=`` override for tests).

    Args:
        file_bytes: The raw resume bytes (PDF / DOCX / TXT / MD).
        filename: The original filename, used to pick the extraction
            backend. The bytes alone aren't enough to pick a parser.
        llm_client: An optional pre-built :class:`LLMClient`. When
            ``None`` (the common case in the resume upload side
            effect), one is constructed via ``LLMClient.from_env()``.
            Tests pass a mock here so they don't need
            ``NVIDIA_API_KEY`` in the environment.

    Returns:
        The validated + saved :class:`Profile`. The on-disk YAML
        is the source of truth after this call returns; the
        returned object is for the caller's logging.

    Raises:
        RuntimeError: If the LLM extraction exhausts every provider
            in the chain. The resume upload route catches this and
            logs it as a side-effect failure; the upload itself
            still returns 201.
        pydantic.ValidationError: If the LLM returned a structurally
            invalid dict (e.g. a list at the top level). Unlikely
            with the strict system prompt but defended against so a
            bad LLM response doesn't silently write a corrupt YAML.
    """
    text = extract_resume_text(file_bytes, filename)
    if not text.strip():
        _logger.warning(
            "no text extracted from %s; saving an empty profile", filename
        )
        empty = Profile()
        save_profile(empty)
        return empty
    truncated = _truncate_resume_text(text)
    if llm_client is None:
        llm_client = LLMClient.from_env()
    data, _model = await llm_client.extract_profile(truncated)
    profile = Profile(**data)
    save_profile(profile)
    return profile


async def _run_profile_extraction_after_upload(
    resume_id: str,
    file_bytes: bytes,
    filename: str,
) -> None:
    """Background-task entry point used by the resume upload route.

    Wraps :func:`extract_profile_from_resume` in a try/except so a
    failed extraction (LLM timeout, bad JSON, all providers down) is
    LOGGED but does NOT crash the FastAPI BackgroundTasks runner. The
    resume upload itself already returned 201 to the client; the
    extraction is best-effort and the operator can re-trigger it
    from ``POST /api/profile/regenerate``.

    Logs are tagged ``jobradar.profile`` so a deploy with
    structured-logging can route them to a dedicated channel.

    Args:
        resume_id: The DB id of the just-uploaded resume. Currently
            unused — reserved for a future ``profile_extraction_status``
            field on the resume record (a Step-2.1 polish that
            surfaces "extraction failed" in the ResumesModal without
            requiring the operator to check logs).
        file_bytes: The raw resume bytes, already in memory because
            the upload route read them once for the size cap.
        filename: The original filename, passed to
            :func:`extract_resume_text` for parser selection.
    """
    try:
        profile = await extract_profile_from_resume(file_bytes, filename)
        _logger.info(
            "profile extracted from resume %s (%s) — %d target roles, %d proof points",
            resume_id,
            filename,
            len(profile.target_roles.primary)
            + len(profile.target_roles.archetypes),
            len(profile.narrative.proof_points),
        )
    except Exception as exc:  # noqa: BLE001 — background-task safety net
        # We intentionally swallow ALL exceptions here. A failed
        # extraction must not crash the BackgroundTasks runner
        # (FastAPI logs the traceback but the upload response was
        # already sent — a raised exception now would just spam
        # logs and confuse operators reading them).
        _logger.exception(
            "profile extraction failed for resume %s (%s): %s",
            resume_id,
            filename,
            type(exc).__name__,
        )


__all__ = [
    "REPO_ROOT",
    "CONFIG_DIR",
    "PROFILE_PATH",
    "EXAMPLE_PATH",
    "MAX_RESUME_CHARS",
    "Candidate",
    "Archetype",
    "TargetRoles",
    "ProofPoint",
    "Narrative",
    "Compensation",
    "Location",
    "Profile",
    "FitLevel",
    "get_profile_path",
    "load_profile",
    "save_profile",
    "reset_cache",
    "get_all_target_roles",
    "get_target_roles_by_fit",
    "build_profile_summary",
    "extract_resume_text",
    "extract_profile_from_resume",
    "_run_profile_extraction_after_upload",
]
