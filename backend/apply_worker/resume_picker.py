"""apply_worker.resume_picker — tag-match → LLM-fallback resume selector.

Pure Python, deterministic when given good inputs. The apply worker
calls :func:`pick_resume` once per job. Two-pass algorithm:

1. **Tag-match** — :func:`derive_role_family_tags` reads the job's
   ``title`` + ``description`` and tags every role-family cue that
   matches (production-AI, AI-security, forward-deployed, data-science,
   mlops). Each candidate :class:`ResumeRecord` is scored by
   ``len(set(resume.tags) & set(role_family_tags))`` with a +0.5 bonus
   for the default resume so a single strong-tag non-default still
   beats a 0-tag default. Highest-scoring resume wins; ties break by
   ``uploaded_at DESC`` so the operator's most recent edit wins.
2. **LLM fallback** — if no resume has any tag overlap, hand the job
   context + a lean ``[{id, name, tags, brief}]`` list to
   :meth:`services.llm_client.LLMClient.pick_best_resume`. The LLM
   decides which resume (if any) best matches the JD.

The LLM fallback fires only when NEITHER (a) tag-match has a winner
NOR (b) the table has an ``is_default=True`` resume to fall back
to. A single refusal path keeps the per-job LLM cost bounded even
on ATS boards where every posting is multi-domain (Ashby often
mixes platform + research + security in one JD).

When both pass-through signals FAIL (no tag overlap, no default
resume, AND the LLM call errors out), :func:`pick_resume` returns
``None`` — the worker surfaces the ``None`` outcome to the operator
as "manually pick a resume, then re-queue this job".
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from apply_worker.types import JobRecord, ResumeRecord
from services.llm_client import LLMClient

_logger = logging.getLogger("jobradar.apply_worker")


# ---------------------------------------------------------------------------
# Role-family keyword map.
#
# Lowercased substring cues. The ``"production-ai"`` family covers
# everything from "Senior ML Engineer" to "LLM Inference Platform
# PM" — the cue is "this role builds the platform that runs models"
# rather than the literal token "production". The
# ``"forward-deployed"`` family deliberately overlaps with
# "Solutions Engineer" + "Customer Engineer" + "Professional
# Services" because these are the SAME job at most companies
# (different title, same work) and the operator's resume for one
# should win for the others.
#
# Adding to this list is a 1-line edit; the picker does not need a
# full config refactor. Operators who want to override the map at
# runtime pass ``custom_family_keywords=`` to :func:`pick_resume`.
# ---------------------------------------------------------------------------
DEFAULT_ROLE_FAMILY_KEYWORDS: dict[str, list[str]] = {
    "production-ai": [
        # Compound phrases anchored with single-space padding for
        # the WORD-BOUNDARY trick used by :func:`derive_role_family_tags`
        # (text is padded with spaces so substring lookup matches
        # standalone tokens). Short bare tokens (``llm``, ``rag``,
        # ``ai``) are intentionally absent — they fire too often
        # on prose (``"hello"`` contains ``"llm"``,
        # ``"available"`` contains ``"ai"``).
        " ai engineer", " ml engineer", " ai platform", " ml platform",
        " ml infrastructure", " ml infra", " inference ", "rag ",
        " retrieval augmented", " fine-tuning", " fine tuning", " model serving",
        " training systems", " model infra",
        " machine learning platform", " ai infra",
    ],
    "ai-security": [
        " ai security", " security ai", " red team", " adversarial",
        " ai safety", " secure ml", " secure ai", " model security",
        " model red team", " responsible ai",
    ],
    "forward-deployed": [
        # Compound phrases + the ``forward-deployed`` hyphenated
        # form (which has no spaces to pad). The standalone
        # ``deployment`` keyword was dropped — it fired too easily
        # on phrases like ``"the deployment of inference"``.
        " forward deployed", "forward-deployed", " solutions engineer",
        " customer engineer", " professional services", " field engineer",
        " customer success engineer", " implementations ",
    ],
    "data-science": [
        " data scientist", " computer vision", " genai",
        " data science", " research scientist", " applied scientist",
        # ``nlp`` is too short to match safely as a bare token —
        # fire only as a phrase.
        " nlp engineer", " nlp scientist", "machine learning research",
    ],
    "mlops": [
        " mlops", " data pipelines", " data platform", " data engineering",
        " kubernetes", " feature store", " model registry", " data infra",
    ],
}


# ``is_default`` bonus added to the overlap score so a strongly-tagged
# non-default resume beats a 0-tag default. Picked empirically —
# bumping this higher means tag-match never overrides the explicit
# default, which is what operators expect when they curate a
# default. Keep it below 1.0 so a single matching tag outranks a
# default that has no signal.
DEFAULT_BONUS = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def derive_role_family_tags(
    *,
    title: str,
    description: str = "",
    family_keywords: dict[str, list[str]] | None = None,
) -> set[str]:
    """Extract role-family tags by substring-matching ``title`` +
    ``description`` against :data:`DEFAULT_ROLE_FAMILY_KEYWORDS`.

    The text is lowercased once for a single substring pass. A family
    is in the result if AT LEAST ONE of its keywords is present in
    the joined text. Empty input yields an empty set. Pure local
    operation — no LLM, no I/O — so testable with a fixed input /
    output mapping in :mod:`tests.test_resume_picker`.
    """
    # Pad with single spaces on both ends so the keyword substring
    # lookup matches standalone tokens: keyword ``" ai engineer"``
    # (leading space) only fires when the joined text actually
    # contains ``" ai engineer"`` as a phrase, not as a substring
    # of ``"available"`` or ``"rain"``. Trade-off vs regex word
    # boundaries: this is O(len(kw)) per keyword (no regex compile),
    # and remains trivially overridable via ``custom_family_keywords=``.
    text = f" {title or ''} {description or ''} ".lower()
    if not text.strip():
        return set()
    families = family_keywords or DEFAULT_ROLE_FAMILY_KEYWORDS
    found: set[str] = set()
    for family, keywords in families.items():
        for kw in keywords:
            if kw and kw in text:
                found.add(family)
                break  # one match is enough — don't double-count within a family
    return found


def _score_resume(
    resume: ResumeRecord,
    family_tags: set[str],
    default_bonus: float = DEFAULT_BONUS,
) -> float:
    """Overlap + (optional) default bonus. Higher is better.

    The +0.5 default keeps the policy deterministic when a curated
    default exists but the job doesn't surface any tag-matchable
    cues — the operator's explicit "this is my default" wins over
    an empty overlap. A single matched tag still beats a 0-tag
    default (1.0 > 0.5), so the picker is sensitive to operator
    tagging discipline.
    """
    overlap = len(set(resume.tags) & family_tags)
    if resume.is_default:
        overlap += default_bonus
    return overlap


def _sorted_by_score(
    resumes: Iterable[ResumeRecord],
    family_tags: set[str],
    default_bonus: float,
) -> list[tuple[ResumeRecord, float]]:
    """Sort resumes by ``(-score, -uploaded_at, name)`` so the highest-scoring
    resume is first and ties break by newest upload (then by name).

    Single ascending sort works because the sort keys are negated for
    the descending dimensions — Python's tuple comparison is
    stable, so ``name`` only matters when score + uploaded_at are
    equal (a near-impossible case in production but useful for test
    reproducibility).
    """
    return sorted(
        ((r, _score_resume(r, family_tags, default_bonus)) for r in resumes),
        key=lambda pair: (
            -pair[1],
            -_timestamp_key(pair[0].uploaded_at),
            pair[0].name,
        ),
    )


def _timestamp_key(ts: str) -> float:
    """Numeric key for ``uploaded_at`` tiebreak, default to 0.0 when empty.

    Built from :func:`datetime.fromisoformat` — strict ISO-8601
    only, so a malformed ``uploaded_at`` sorts to 0 (last). Empty
    strings return 0 rather than raising; malformed strings are
    silently coerced via ``errors='replace'`` on the date part.
    """
    if not ts:
        return 0.0
    try:
        # Replace non-ISO trailing ``Z`` with ``+00:00`` so
        # ``fromisoformat`` parses. Python's stdlib added ``Z`` support
        # in 3.11, but the replace is cheap and explicit.
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def pick_resume(
    job: dict[str, Any] | JobRecord,
    resumes: list[dict[str, Any] | ResumeRecord] | None,
    *,
    llm_client: LLMClient | None = None,
    custom_family_keywords: dict[str, list[str]] | None = None,
    default_bonus: float = DEFAULT_BONUS,
    require_llm_fallback: bool = False,
) -> ResumeRecord | None:
    """Pick the best resume for a single job. ``None`` only when

    * there are no resumes at all, OR
    * tag-match did not hit, no default resume exists, AND
      ``require_llm_fallback=False`` (the default — and we never
      silently spend the operator's LLM budget on a no-cue job).

    Args:
        job: ``JobRecord`` OR plain ``dict`` (``title`` + ``description``
            are the only fields read). Extra keys are ignored.
        resumes: Candidate :class:`ResumeRecord` list, plain-dict
            equivalent, OR ``None`` for "no resumes uploaded yet".
        llm_client: Optional :class:`services.llm_client.LLMClient`.
            When ``None``, the LLM fallback path raises
            :class:`RuntimeError` if it ever fires. Production code
            passes ``LLMClient.from_env()``; tests pass an
            ``AsyncMock``.
        custom_family_keywords: Override :data:`DEFAULT_ROLE_FAMILY_KEYWORDS`
            for a single call. Useful when the operator onboards a
            new domain (e.g. ``"data-engineering"``) without a code
            change.
        default_bonus: Adjust the per-call weight of the
            ``is_default`` flag. ``0.0`` disables the bonus (pure
            overlap ranks resumes); ``1.0`` makes a default always
            outrank a non-default (even when the non-default has
            more tag overlap). The default ``0.5`` is the
            policy sweet-spot.
        require_llm_fallback: When ``True``, force an LLM call even
            when tag-match has a confident winner — useful for
            benchmarking the LLM accuracy against the heuristic on
            a representative job sample. Not used in production.

    Returns:
        The chosen :class:`ResumeRecord`, or ``None`` if no
        candidate could be picked.
    """
    job_record = (
        job if isinstance(job, JobRecord) else JobRecord.from_data(job)
    )
    resume_pool: list[ResumeRecord] = []
    for r in resumes or []:
        resume_pool.append(
            r if isinstance(r, ResumeRecord) else ResumeRecord.from_data(r)
        )

    if not resume_pool:
        _logger.info("pick_resume: no resumes uploaded; returning None")
        return None

    # ----- Pass 1: tag match ----------------------------------------------
    family_tags = derive_role_family_tags(
        title=job_record.title,
        description=job_record.description,
        family_keywords=custom_family_keywords,
    )
    scored = _sorted_by_score(resume_pool, family_tags, default_bonus)
    top_resume, top_score = scored[0]

    # Decision logic — keep it auditable:
    #   family_tags fires + top_score > 0   → tag-match winner.
    #   family_tags empty + top_score > 0  → top score came purely
    #                                         from the default bonus on
    #                                         a resume that itself has 0
    #                                         family tags. That's the
    #                                         operator's "default" kicking
    #                                         in — accept it.
    #   family_tags empty + top_score == 0 → everyone has 0 tags and
    #                                         no one is default. Fall
    #                                         through to LLM (or None).
    if top_score > 0:
        selected = top_resume
        _logger.info(
            "pick_resume: tag-match chose resume %s (score=%.2f, "
            "tags_overlap=%d, family_tags=%s)",
            selected.id,
            top_score,
            len(set(selected.tags) & family_tags),
            sorted(family_tags),
        )
        return selected
    if top_resume.is_default and not family_tags and not require_llm_fallback:
        # Even if no tags fired, the operator wrote a default.
        # Surface that as the choice rather than burning an LLM call
        # on a no-signal job (LLM is more useful elsewhere).
        _logger.info(
            "pick_resume: no tag-match, falling back to is_default resume %s",
            top_resume.id,
        )
        return top_resume

    # ----- Pass 2: LLM fallback -------------------------------------------
    if llm_client is None:
        _logger.info(
            "pick_resume: no tag-match, no default — returning None "
            "(no llm_client supplied)"
        )
        return None

    lean_payload = [
        {
            "id": r.id,
            "name": r.name,
            "tags": r.tags,
            "is_default": r.is_default,
            "uploaded_at": r.uploaded_at,
        }
        for r in resume_pool
    ]
    job_payload = {
        "title": job_record.title,
        "description": job_record.description,
        "company_name": job_record.company_name,
        "ats_type": job_record.ats_type,
    }
    try:
        chosen_id, confidence = await llm_client.pick_best_resume(
            job_payload, lean_payload
        )
    except Exception as exc:  # noqa: BLE001 — see LLM fallback contract
        _logger.warning(
            "pick_resume: LLM fallback failed (%s); returning None",
            type(exc).__name__,
        )
        return None
    if not chosen_id:
        _logger.info(
            "pick_resume: LLM returned no resume id (confidence=%.2f); "
            "returning None",
            confidence,
        )
        return None
    chosen = next((r for r in resume_pool if r.id == chosen_id), None)
    if chosen is None:
        _logger.warning(
            "pick_resume: LLM returned id=%s but no matching resume in pool; "
            "returning None",
            chosen_id,
        )
        return None
    _logger.info(
        "pick_resume: LLM fallback chose resume %s (confidence=%.2f)",
        chosen.id,
        confidence,
    )
    return chosen


__all__ = [
    "DEFAULT_ROLE_FAMILY_KEYWORDS",
    "DEFAULT_BONUS",
    "derive_role_family_tags",
    "pick_resume",
]
