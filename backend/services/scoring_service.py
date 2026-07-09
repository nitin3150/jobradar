"""Profile-aware opportunity scoring + threshold-filter persistence to Supabase Postgres.

Reads
=====

* :data:`routes.settings._PREFS_STATE` — in-memory singleton the same
  :mod:`routes.settings` writes to. We read ``job_fit_threshold`` and
  ``target_roles``. (Will move to :class:`models.Preferences` once
  :mod:`routes.settings` itself goes DB-native.)
* :data:`routes.qa_bank._QA_DB` — in-memory seeded Q&A store. We pull
  up to :data:`MAX_QA_ENTRIES_IN_PROMPT` entries, trimmed to
  :data:`MAX_CHARS_PER_QA_ANSWER` chars each, so the prompt stays bounded.

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
import hashlib
import json
import logging
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from db import models as db_models
from db.session import AsyncSessionLocal, require_database_configured
from routes.qa_bank import _QA_DB
from routes.settings import _PREFS_STATE

from .llm_client import LLMClient

logger = logging.getLogger("jobradar.scoring")

# Caps on the profile-summary prompt so the LLM context stays bounded
# regardless of how many Q&A entries the operator accumulates. Extra
# entries add tokens without changing the model's calibrated answer.
MAX_QA_ENTRIES_IN_PROMPT = 12
MAX_CHARS_PER_QA_ANSWER = 200
MAX_SCORE_CONCURRENCY = 8


def build_profile_summary() -> str:
    """Compose the candidate-context blob the LLM scores against.

    Format::

        Target roles:
        - AI Engineer
        - LLM Engineer
        ...

        Q&A summary (frequently asked application questions):
        - Q: Years of experience
          A: 5 years of professional software engineering ...
        - Q: ...
    """
    prefs = _PREFS_STATE.get("data") or {}
    roles = prefs.get("target_roles") or []
    qa_entries = list(_QA_DB.values())

    parts: list[str] = []
    if roles:
        parts.append("Target roles:\n" + "\n".join(f"- {r}" for r in roles))
    if qa_entries:
        sorted_entries = sorted(
            qa_entries,
            key=lambda e: (-e.get("times_used", 0), e["canonical_question"]),
        )
        sorted_entries = sorted_entries[:MAX_QA_ENTRIES_IN_PROMPT]
        qa_lines: list[str] = []
        for entry in sorted_entries:
            answer = (entry.get("answer") or "(no answer yet)").strip()[:MAX_CHARS_PER_QA_ANSWER]
            qa_lines.append(f"- Q: {entry['canonical_question']}\n  A: {answer}")
        parts.append("Q&A summary:\n" + "\n".join(qa_lines))
    return "\n\n".join(parts) if parts else "(no profile configured)"


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
        "status": "in_review",
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
        # Scheduler populates ``review_deadline`` when it processes
        # the queue; out of scope here. ``None`` keeps terminal
        # statuses consistent with the Pydantic model in routes.jobs.
        "review_deadline": None,
    }


UPSERT_UPDATE_COLUMNS = (
    "status",
    "ats_type",
    "title",
    "company_name",
    "url",
    "ai_fit_score",
    "ai_fit_reasoning",
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
            except RuntimeError as exc:
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
    "score_and_persist",
    "_score_and_persist_async",
    "MAX_QA_ENTRIES_IN_PROMPT",
    "MAX_CHARS_PER_QA_ANSWER",
]
