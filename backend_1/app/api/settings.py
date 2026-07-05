"""`GET / PATCH /settings` — single-tenant user preferences, singleton row."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    DEFAULT_JOB_FIT_THRESHOLD,
    DEFAULT_REVIEW_WINDOW_HOURS,
    DEFAULT_SEND_FOLLOWUP_EMAILS,
    DEFAULT_TARGET_ROLES,
    Preferences,
)
from app.schemas.preferences import PreferencesResponse, PreferencesUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


async def get_or_seed_preferences(db: AsyncSession) -> Preferences:
    """Return the singleton row, inserting defaults if it doesn't exist yet.

    Race-safe: if two callers hit an empty DB concurrently, the `id=1` PK
    surfaces on the second commit and we re-read instead of erroring.
    """
    prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
    if prefs is not None:
        return prefs

    seeded = Preferences(
        id=Preferences.SINGLETON_ID,
        target_roles=list(DEFAULT_TARGET_ROLES),
        review_window_hours=DEFAULT_REVIEW_WINDOW_HOURS,
        job_fit_threshold=DEFAULT_JOB_FIT_THRESHOLD,
        send_followup_emails=DEFAULT_SEND_FOLLOWUP_EMAILS,
    )
    db.add(seeded)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
        if prefs is None:
            # Don't swallow a real failure.
            raise HTTPException(status_code=500, detail="Could not seed preferences")
        return prefs
    await db.refresh(seeded)
    logger.info("Seeded default preferences row (id=%d)", seeded.id)
    return seeded


def _to_response(p: Preferences) -> PreferencesResponse:
    return PreferencesResponse(
        target_roles=list(p.target_roles or []),
        review_window_hours=p.review_window_hours,
        job_fit_threshold=p.job_fit_threshold,
        send_followup_emails=p.send_followup_emails,
        updated_at=p.updated_at,
    )


@router.get("", response_model=PreferencesResponse)
async def get_preferences(db: AsyncSession = Depends(get_db)) -> PreferencesResponse:
    prefs = await get_or_seed_preferences(db)
    return _to_response(prefs)


@router.patch("", response_model=PreferencesResponse)
async def update_preferences(
    payload: PreferencesUpdate,
    db: AsyncSession = Depends(get_db),
) -> PreferencesResponse:
    prefs = await get_or_seed_preferences(db)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        # Nothing to change — keep response cheating as cheap as the GET path.
        return _to_response(prefs)

    for field, value in updates.items():
        if field == "target_roles" and isinstance(value, list):
            # Normalize: trim, lower-case, dedupe, drop blanks.
            seen: set[str] = set()
            normalized: list[str] = []
            for role in value:
                if not isinstance(role, str):
                    continue
                norm = role.strip().lower()
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                normalized.append(norm)
            prefs.target_roles = normalized
        else:
            setattr(prefs, field, value)

    # onupdate=func.now() will refresh updated_at server-side.
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Could not update preferences")
    await db.refresh(prefs)
    logger.info(
        "Preferences updated: fields=%s", sorted(updates.keys())
    )
    return _to_response(prefs)
