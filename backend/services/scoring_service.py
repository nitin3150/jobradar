"""Profile-aware opportunity scoring + threshold-filter persistence to Supabase Postgres.

Reads
=====

* :data:`routes.settings._PREFS_STATE` — in-memory singleton the same
  :mod:`routes.settings` writes to. We read ``job_fit_threshold``
  (the only knob the operator tunes for scoring). Target roles,
  archetypes, and narrative context now come from the YAML profile
  via :mod:`services.profile_service`, NOT from this dict.
* :func:`services.profile_service.build_profile_summary` — renders
  the rich ``config/profile.yml`` (target roles, archetypes,
  narrative, compensation, location) into the markdown block the
  LLM scoring prompt uses. Replaces the old Q&A-bank-blended
  summary that mixed ``_PREFS_STATE.target_roles`` with
  ``_QA_DB.values()``.

Writes
======

Above-threshold winners are UPSERTed into the ``jobs`` table via
``INSERT ... ON CONFLICT (id) DO UPDATE``. The ``id`` is a
:func:`uuid.uuid5`-derived deterministic value
(``uuid5(NAMESPACE_URL, f"{ats_type}:{url}")``) so re-running a scan
overwrites the prior entry rather than appending a clone.

The DB upsert swallows failures at WARN — an unreachable Postgres
must not break the scanner response. We return the integer winners
count either way so the route can echo it back to the operator.

Failure handling
================

* If neither LLM provider is configured, :func:`score_and_persist` returns
  ``0`` with a WARN log; we don't break the scanner response.
* If the LLM errors on a single opportunity, that opportunity is dropped
  and we move on — no exception escapes to the caller.
* If the DB upsert fails, the row is dropped and we WARN — the scan
  response still ships.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from db import models as db_models
from db.session import AsyncSessionLocal, require_database_configured
from routes.settings import _PREFS_STATE

from services import profile_service
from .llm_client import LLMClient

logger = logging.getLogger("jobradar.scoring")

# Cap on the number of opportunity-scoring coroutines running in
# parallel inside one scanner pass. The LLM client has its own
# per-provider rate-limiter (``AsyncTokenBucket`` sized at NVIDIA_RPM
# by default), so this concurrency limit is a separate concern: it
# caps the in-flight coroutine count so a bulk scan doesn't queue
# hundreds of open ``await client.score_opportunity(...)`` calls on
# the same event loop. 8 is empirically enough to saturate a 40-RPM
# NVIDIA key without overwhelming the loop scheduler.


# -----------------------------------------------------------------------
# v0.7.x visa-flag detection helpers.
#
# The LLM scoring prompt (:data:`services.llm_client.SYSTEM_PROMPT`) tells
# the model to prefix its `reasoning` JSON value with one of four canonical
# tags (``visa_flag:positive``, ``visa_flag:negative``,
# ``visa_flag:ambiguous``, ``visa_flag:none``) so downstream code can
# extract the visa signal deterministically without parsing free-form
# English. We do that twice: once here, once via real-time clamping in
# :func:`apply_visa_calibration`.
# -----------------------------------------------------------------------


_VISA_FLAG_RE = re.compile(
    r"""^\s*visa_flag\s*:\s*
        (?P<flag>positive|negative|ambiguous|none)\b
        \s*[\-:\u2013\u2014\s]*       # optional separator: - \u2013 \u2014 : space
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_visa_flag(reasoning: str) -> tuple[str | None, str]:
    """Pull the ``visa_flag:X`` prefix off an LLM ``reasoning`` value.

    Returns ``(flag, cleaned_reasoning)`` where ``flag`` is one of
    ``"positive"`` / ``"negative"`` / ``"ambiguous"`` / ``"none"``
    (lowercased) when the prefix is present, else ``None``.
    ``cleaned_reasoning`` is the input with the prefix (and any
    trailing separator characters) stripped and re-stripped; the
    identity mapping when no prefix is present.

    Why the prefix: the JSON envelope is fixed-shape
    (``{"score": float, "reasoning": str}``); injecting a third key
    would break ``_JSON_OBJ_RE`` in :mod:`services.llm_client`. Putting
    the flag at the start of reasoning is parseable by a single
    anchored regex, modeled on the production-standard
    ``Sent: 2026-01-01 08:00 ...`` style.
    """
    if not reasoning:
        return None, ""
    match = _VISA_FLAG_RE.match(reasoning)
    if not match:
        return None, reasoning
    flag = match.group("flag").lower()
    cleaned = reasoning[match.end():].strip()
    return flag, cleaned


# Cap values used by ``apply_visa_calibration`` are intentionally
# conservative: optimistic boosts cap at 0.95 and pessimistic clamps
# floor at 0.2. Both come from the SYSTEM_PROMPT clause 6 calibration
# guidance ("score 0.0-0.2 for hard-mismatch, nudge +0.05 for positive
# signal cap 0.95"). They mirror the same boundaries the LLM is told
# to apply, so when the LLM already calibrated we don't fight it.
_VISA_CALIBRATION_POSITIVE_CAP = 0.95
_VISA_CALIBRATION_NEGATIVE_FLOOR = 0.2
_VISA_CALIBRATION_POSITIVE_BOOST = 0.05


def apply_visa_calibration(
    score: float, visa_flag: str | None, visa_status: str | None
) -> float:
    """Belt-and-suspenders score adjustment based on visa_flag x visa_status.

    The LLM is told (and we have heuristics on the description doing the
    same job) what to do with each combination, but models err. This
    post-processor clamps the LLM's output deterministically so a
    runaway phrase like "should sponsor your visa" can't smuggle a
    mismatch past the threshold, AND so the LLM can't smuggle an
    out-of-range score (1.5 / -0.5) past the DB clamp.

    Calibration matrix:

    * Candidate ``visa_status`` says "needs sponsor" (e.g.
      ``"No sponsorship"`` or any lowercase variant containing
      ``"no sponsorship"``) AND
        * LLM-issued flag is ``"negative"``    -> score floored at 0.2.
        * LLM-issued flag is ``"positive"``    -> score boosted by 0.05,
          capped at 0.95 (avoid overweighting a single signal).
    * Candidate ``visa_status`` says "doesn't need sponsor" (e.g.
      ``"No sponsorship needed"``) AND any flag -> score unchanged.
    * Either side unknown / None -> score unchanged.

    Returns the calibrated score clamped to ``[0.0, 1.0]``.
    """
    if visa_flag is None or visa_status is None:
        return max(0.0, min(1.0, score))

    # Strip whitespace so an operator's stray newline or tab in their
    # profile.yml doesn't silently drop the calibration (SHOULD-FIX #1
    # from the 2026-07 code review).
    visa_status = visa_status.strip()

    candidate_needs_sponsor = (
        "no sponsorship" in visa_status.lower()
        and "needed" not in visa_status.lower()
    )
    candidate_has_sponsor = "needed" in visa_status.lower()

    flag = visa_flag.lower()

    if candidate_has_sponsor:
        # The candidate is already authorised. Visa clauses are not
        # material to the score; let the LLM's reasoning stand.
        calibrated = score
    elif candidate_needs_sponsor:
        if flag == "negative":
            # Hard mismatch ceiling per the SYSTEM_PROMPT clause 6.
            calibrated = min(_VISA_CALIBRATION_NEGATIVE_FLOOR, score)
        elif flag == "positive":
            calibrated = min(
                _VISA_CALIBRATION_POSITIVE_CAP,
                score + _VISA_CALIBRATION_POSITIVE_BOOST,
            )
        else:
            # ``ambiguous`` and ``none`` are informational only — we
            # don't move the score on visa factors alone.
            calibrated = score
    else:
        calibrated = score

    # Final clamp: an LLM that returned 1.5 or -0.5 should still 
    return max(0.0, min(1.0, calibrated))

def _get_candidate_visa_status() -> str | None:
    """Read the candidate's ``visa_status`` from the loaded profile, or None.

    Sync helper so :func:`_score_and_persist_async` can apply
    :func:`apply_visa_calibration` without awaiting. The profile
    service caches the loaded profile at module level so repeated
    calls within one scan are cheap.

    Returns ``None`` when no profile is configured (the scorer
    already passes ``"(no profile configured)"`` to the LLM in that
    case) so the calibration step becomes a no-op via its own guard.
    """
    try:
        profile = profile_service.load_profile(use_cache=True)
    except Exception as exc:  # noqa: BLE001 — disk / parse failure
        logger.debug("could not load profile for visa_status: %s", exc)
        return None
    if profile is None:
        return None
    location = getattr(profile, "location", None)
    if location is None:
        return None
    return getattr(location, "visa_status", None)

MAX_SCORE_CONCURRENCY = 8


def build_profile_summary() -> str:
    """Compose the candidate-context blob the LLM scores against.

    Step-3: delegates to :func:`services.profile_service.build_profile_summary`
    so the scoring context comes from the operator's YAML profile
    (target roles, archetypes, narrative, compensation, location)
    rather than the Q&A bank. The Q&A bank is reserved for the
    application form auto-fill (see :mod:`routes.qa_bank`) — it
    wasn't pulling its weight in the scoring prompt (the LLM
    couldn't use "Years of experience: 5" to decide whether a
    job is a fit) and it crowded out the rich profile context.

    The public function name + zero-arg signature is preserved so
    callers (the boards runner, the test suite, the legacy
    ``scoring_service.build_profile_summary()`` consumers) keep
    working without modification.
    """
    return profile_service.build_profile_summary()


def _job_id(ats_type: str, opportunity: dict[str, Any]):
    """Stable UUID5-derived id so re-running a scan overwrites rather than appends clones.

    Also matches the URL-discriminator convention used by the boards
    runner's ``(board, job_id)`` dedupe keys — same input string
    shape, same UUID namespace, so future dedupe work on the
    :class:`models.BoardSeenJob` table can share the formula verbatim.
    """
    url = (
        opportunity.get("url")
        or opportunity.get("external_id")
        or json.dumps(opportunity, sort_keys=True, default=str)
    )
    return uuid5(NAMESPACE_URL, f"{ats_type}:{url}")


def _opportunity_to_job_fields(
    opp: dict[str, Any], ats_type: str, score: float, reasoning: str, job_id
) -> dict[str, Any]:
    """Map an opportunity dict to the column shape :class:`models.Job` consumes.

    The model's FK column ``company_id`` is left NULL — board-level
    dedupe first lands here as a workspace for the outreach flow
    which may later link to the :class:`models.Company` table.
    """
    return {
        "id": job_id,
        # Single-threshold rule: every opportunity that cleared
        # ``job_fit_threshold`` is written directly with
        # ``status='approved'``. The apply worker (or the operator)
        # picks ``approved`` jobs up on the next polling tick. There
        # is no ``in_review`` intermediate — the LLM scoring
        # decision IS the approval decision. Below-threshold jobs
        # are filtered out in ``_score_one`` and never reach this
        # mapping. Route-level approve/reject endpoints in
        # ``routes.jobs`` now never receive ``in_review`` rows from
        # the scorer either — they are reserved for operator
        # re-classification of already-approved / applied rows.
        "status": "approved",
        "ats_type": ats_type,
        "title": opp.get("title") or opp.get("name") or "(untitled)",
        "company_name": (
            opp.get("company_name")
            or opp.get("organization")
            or opp.get("company")
            or "(unknown)"
        ),
        "url": opp.get("url") or "(no url)",
        "ai_fit_score": round(max(0.0, min(1.0, score)), 4),
        "ai_fit_reasoning": reasoning,
        # Board-published posting body. The boards runner (Greenhouse,
        # Lever, Ashby) extracts ``description`` from each response
        # payload so the React ``JobCard`` can render a truncated
        # preview + a "Read more" modal without re-fetching. The
        # column is nullable because Ashby sometimes omits the field.
        "description": opp.get("description") or None,
        # Scheduler populates ``review_deadline`` when it processes
        # the queue; out of scope here. ``None`` keeps terminal
        # statuses consistent with the Pydantic model in routes.jobs.
        "review_deadline": None,
    }


# UPSERT_UPDATE_COLUMNS deliberately omits ``status``.
#
# Why status is NOT in the update-set
# ===================================
# The single-threshold ``status='approved'`` decision is correct on a
# FRESH insert — every above-threshold winner lands directly in the
# apply-queue pool, no ``in_review`` intermediate. But the SECOND
# time a job is scored (e.g. a local UI re-scan via
# ``POST /api/scan/boards``, or any future path that re-sends an
# existing winner through the LLM), the SQLAlchemy path's
# ``INSERT ... ON CONFLICT (id) DO UPDATE`` would otherwise force
# ``status='approved'`` onto a row the operator may have already
# triaged to ``rejected``, ``applied``, ``paused``, ``flagged``, or
# any other terminal/in-flight state. That silent flip is the bug
# the operator reported: today's run "deletes" yesterday's triage
# by force-resetting every None-``approved`` row back to ``approved``.
#
# Fix: only update content/scoring columns on conflict. ``status``
# is set ONCE at insert time (from :func:`_opportunity_to_job_fields`)
# and never touched again. The operator's triage decisions survive
# any number of subsequent scoring runs. The route-level
# approve/reject endpoints (``PATCH /api/jobs/{id}/status`` etc.)
# remain the only writer of the ``status`` column post-insert.
UPSERT_UPDATE_COLUMNS = (
    "ats_type",
    "title",
    "company_name",
    "url",
    "ai_fit_score",
    "ai_fit_reasoning",
    "description",
)


async def _persist_winners(
    winners: list[tuple[dict[str, Any], float, str]], ats_type: str
) -> int:
    """INSERT … ON CONFLICT (id) DO UPDATE for each winner; commit as one tx.

    Returns the count of rows the upsert actually wrote (= number of
    winners) so the caller can log + return an integer. DB errors
    raise — the outer caller has a try/except to surface them.
    """
    require_database_configured()
    assert AsyncSessionLocal is not None  # noqa: S101

    if not winners:
        return 0

    async with AsyncSessionLocal() as session:
        for opp, score, reasoning in winners:
            job_id = _job_id(ats_type, opp)
            row_dict = _opportunity_to_job_fields(
                opp, ats_type, score, reasoning, job_id
            )
            stmt = pg_insert(db_models.Job).values(**row_dict)
            update_set = {
                col: getattr(stmt.excluded, col) for col in UPSERT_UPDATE_COLUMNS
            }
            update_set["updated_at"] = func.now()
            stmt = stmt.on_conflict_do_update(
                index_elements=[db_models.Job.id],
                set_=update_set,
            )
            await session.execute(stmt)
        await session.commit()
    return len(winners)


async def _score_and_persist_async(
    opportunities: list[dict[str, Any]], ats_type: str
) -> int:
    """Async pipeline: score each opp in parallel, threshold-filter, persist.

    All three phases (score, threshold, persist) run inside this single
    async function so :func:`score_and_persist` only needs one
    ``asyncio.run`` call and the test suite can directly ``await``
    the inner pipeline when running in an async test context.
    """
    if not opportunities:
        return 0

    threshold = float(_PREFS_STATE.get("data", {}).get("job_fit_threshold", 0.6))
    profile = build_profile_summary()
    # v0.7.x: cache the candidate's visa_status once per scan so every
    # per-opportunity calibration call doesn't hit the YAML cache.
    _candidate_visa_status = _get_candidate_visa_status()

    try:
        client = LLMClient.from_env()
    except RuntimeError as exc:
        logger.warning("scoring skipped for %s: %s", ats_type, exc)
        return 0

    semaphore = asyncio.Semaphore(MAX_SCORE_CONCURRENCY)

    async def _score_one(
        opp: dict[str, Any],
    ) -> tuple[dict[str, Any], float, str] | None:
        async with semaphore:
            try:
                score, reasoning = await client.score_opportunity(profile, opp)
                # v0.7.x: pull the LLM-issued visa_flag tag off the
                # reasoning prefix and apply the deterministic
                # calibration matrix (belt-and-suspenders to the LLM
                # which already calibrated per the SYSTEM_PROMPT).
                visa_flag, clean_reasoning = extract_visa_flag(reasoning)
                score = apply_visa_calibration(
                    score, visa_flag, _candidate_visa_status,
                )
                reasoning = clean_reasoning
            except (RuntimeError, asyncio.CancelledError, asyncio.TimeoutError) as exc:
                # Catch operational failures only — LLM transient errors
                # (RuntimeError raised by score_opportunity when all
                # providers fail), worker shutdown (CancelledError
                # mid-rate-limiter-wait), and per-call timeouts
                # (TimeoutError). Shape mismatches like TypeError /
                # AttributeError from a malformed opportunity dict are
                # NOT caught here — those are programming bugs that
                # should surface as test failures, not silent drops.
                logger.debug("scoring failed for %r: %s", opp.get("url"), exc)
                return None
            if score < threshold:
                return None
            return opp, score, reasoning

    
    results = await asyncio.gather(*[_score_one(opp) for opp in opportunities])
    winners = [r for r in results if r is not None]

    if winners:
        try:
            await _persist_winners(winners, ats_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scoring: failed to persist %d winners to DB (%s): %s. "
                "Scores are still returned up to the route, but the "
                "rows are dropped.",
                len(winners),
                ats_type,
                exc,
            )
            return 0

        logger.info(
            "scoring: %d winners / %d total (%s above threshold %.2f)",
            len(winners),
            len(opportunities),
            ats_type,
            threshold,
        )
    return len(winners)


def score_and_persist(
    opportunities: list[dict[str, Any]], ats_type: str
) -> int:
    """Sync wrapper around :func:`_score_and_persist_async` for sync routes.

    Runs the async pipeline in a fresh event loop. All errors — LLM
    mis-config, LLM scoring failures, DB upsert failures — are
    swallowed at the WARN level so a flaky backend doesn't break the
    scanner endpoint's response — the user still gets back their raw
    opportunities.
    """
    try:
        return asyncio.run(_score_and_persist_async(opportunities, ats_type))
    except Exception as exc:  # noqa: BLE001
        logger.warning("score_and_persist failed for %s: %s", ats_type, exc)
        return 0


__all__ = [
    "build_profile_summary",
    "extract_visa_flag",
    "apply_visa_calibration",
    "score_and_persist",
    "_score_and_persist_async",
]
