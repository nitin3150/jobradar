"""Build the candidate profile string that drives job-fit scoring.

`compose_profile` is pure (unit-tested). `build_candidate_profile` is the thin
async DB wrapper used by the job pipeline.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferences import Preferences
from app.models.resume import Resume


def compose_profile(resume_text: str | None, target_roles: list[str]) -> str:
    """Compose a profile string from resume text + target roles.

    Returns "" when there is nothing to say — the scorer then falls back to its
    own default profile.
    """
    parts: list[str] = []
    if target_roles:
        parts.append("Target roles: " + ", ".join(target_roles))
    if resume_text and resume_text.strip():
        parts.append("Resume:\n" + resume_text.strip())
    return "\n\n".join(parts)


async def build_candidate_profile(db: AsyncSession) -> str:
    """Fetch the default resume text + preference roles, compose the profile."""
    res = await db.execute(select(Resume).where(Resume.is_default.is_(True)))
    resume = res.scalar_one_or_none()
    resume_text = resume.extracted_text if resume else None

    prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
    roles = list(prefs.target_roles) if prefs and prefs.target_roles else []

    return compose_profile(resume_text, roles)
