"""Pick the best resume for an approved job.

Selection algorithm (small N, no SQL array gymnastics needed):

  score = (overlap_count, is_default, uploaded_at)
  overlap_count = |tokens(job.title + Settings.target_roles) ∩ tokens(resume.tags)|

The first key wins: a resume whose tags actually overlap with the role wins
over a default that doesn't match. Default wins on ties (chooses between
non-matching tag overlaps), and most-recent wins on identical status.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Preferences, Resume

logger = logging.getLogger(__name__)

# Words that should never participate in overlap scoring — common ATS nouns.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "with", "to", "of", "in", "on",
    "at", "by", "is", "be", "as", "from",
}

# Allow {alphanumeric+space+dot-comma-dash} pass-through for split-on-
# whitespace tokenization. Everything else becomes whitespace.
_TOKEN_RE = re.compile(r"[^\w\s.+-]")


def _tokenize(text: str) -> set[str]:
    """Lowercase, drop punctuation, naive singular stem, drop stopwords."""
    if not text:
        return set()
    cleaned = _TOKEN_RE.sub(" ", text.lower())
    out: set[str] = set()
    for w in cleaned.split():
        if not w or w in _STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


async def _role_tokens(db: AsyncSession, job_title: str) -> set[str]:
    """Tokens from job.title + the configured target_roles (Settings row)."""
    tokens = _tokenize(job_title)
    try:
        prefs = await db.get(Preferences, Preferences.SINGLETON_ID)
        if prefs and prefs.target_roles:
            for role in prefs.target_roles:
                tokens.update(_tokenize(role))
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("Could not load Preferences for resume selector: %s", e)
    return tokens


def _resume_path(storage_filename: str) -> Path:
    """Server-side storage filename → absolute filesystem path.

    Playwright's `set_input_files` requires absolute paths.
    """
    return (Path(settings.resume_storage_dir) / storage_filename).resolve()


async def pick_resume_for_job(
    db: AsyncSession,
    job,
) -> Path | None:
    """Return the absolute filesystem path of the resume to attach, or None.

    Returns None when there are no resumes on file. Callers should treat None
    as "no resume attached" rather than an error.

    `job` is the SQLAlchemy `Job` row — must have `.title` and ideally a
    relationship to the company if you want to score on industry later.
    """
    rows = (await db.execute(select(Resume))).scalars().all()
    if not rows:
        return None

    role_tokens = await _role_tokens(db, job.title or "")

    def score(r: Resume) -> tuple[int, int, float]:
        r_tokens = _tokenize(" ".join(r.tags or []))
        overlap = len(role_tokens & r_tokens)
        # Resolution trick: uploaded_at is timezone-aware datetime in the DB,
        # fall back to .timestamp() for SQLite/test backends.
        ts = r.uploaded_at.timestamp() if r.uploaded_at else 0.0
        return (overlap, int(bool(r.is_default)), ts)

    chosen = max(rows, key=score)
    logger.info(
        "Resume pick: id=%s name=%s tags=%s overlap_with_role=%d is_default=%s",
        chosen.id, chosen.name, chosen.tags, score(chosen)[0], chosen.is_default,
    )
    return _resume_path(chosen.storage_path)
