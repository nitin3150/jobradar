"""Tests for `apply_worker/resume_selector.py`.

The selector pulls every Resume row from the DB and ranks them in-memory.
We don't need a real DB — we stub an async session whose `execute().scalars().all()`
returns a hand-built list of `Resume`-shaped objects. The ranking function
itself is the contract we want to pin down.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apply_worker.resume_selector import _tokenize, pick_resume_for_job
from app.models.preferences import Preferences


def _row(
    *,
    id_: str = "r1",
    tags: list[str] | None = None,
    is_default: bool = False,
    uploaded_at=None,
    storage_path: str | None = None,
):
    """Minimal stand-in for a Resume SQLAlchemy row.

    `storage_path` defaults to `<id_>.pdf` so tests can assert on the picked
    resume's `.name` without re-deriving the path manually.
    """
    return SimpleNamespace(
        id=id_,
        name=f"{id_}.pdf",
        storage_path=storage_path if storage_path is not None else f"{id_}.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        tags=list(tags or []),
        is_default=is_default,
        uploaded_at=uploaded_at,
    )


class _FixedDatetime:
    """Stand-in for a timezone-aware datetime with a deterministic timestamp."""
    def __init__(self, ts: float):
        self._ts = ts

    def timestamp(self) -> float:
        return self._ts


def _fake_async_session(rows: list):
    """Build an async session whose `execute(...).scalars().all()` returns `rows`."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    session.execute.return_value = result
    # `db.get(Preferences, 1)` returns a fixed Preferences row with NO target_roles
    # by default — tests that need target_roles override via side_effect.
    default_prefs = SimpleNamespace(
        id=Preferences.SINGLETON_ID,
        target_roles=[],
    )
    session.get.return_value = default_prefs
    return session


# --- tokenizer ---------------------------------------------------------------


def test_tokenize_lowercases_and_drops_punctuation() -> None:
    # Lower-cased + punctuation replaced; final token stream is lowercase.
    assert "ai" in _tokenize("AI Engineer, Sr.")
    assert "engineer" in _tokenize("AI Engineer, Sr.")
    # 'Sr.' → 'sr.' (the trailing dot is preserved by the token splitter).
    assert "sr." in _tokenize("AI Engineer, Sr.")


def test_tokenize_strips_trivial_plural() -> None:
    # Naive stem: drop trailing 's' on words >3 chars that don't end in 'ss'.
    assert "engineer" in _tokenize("engineers")
    # 'classes' loses the final 's' once → 'classe'. Consistent across both
    # sides of overlap scoring, so job-title-stem == resume-tag-stem matches.
    assert "classe" in _tokenize("classes")
    # Tokens are at least 3 chars long after stem.
    assert all(len(w) >= 3 for w in _tokenize("engineers") | _tokenize("classes"))


def test_tokenize_drops_stopwords() -> None:
    assert "with" not in _tokenize("with the and or")
    assert "ai" in _tokenize("with AI")


def test_tokenize_handles_empty_input() -> None:
    assert _tokenize("") == set()
    assert _tokenize(None or "") == set()


# --- selector: returns None when no resumes --------------------------------


@pytest.mark.asyncio
async def test_pick_returns_none_when_no_resumes() -> None:
    db = _fake_async_session([])
    job = SimpleNamespace(title="Senior ML Engineer")
    result = await pick_resume_for_job(db, job)
    assert result is None


# --- selector: ranking ------------------------------------------------------


@pytest.mark.asyncio
async def test_overlap_beats_default_when_default_doesnt_match() -> None:
    """A tagged resume that matches the role ranks above a non-matching default."""
    tagged = _row(
        id_="tagged", tags=["ml", "engineer"], is_default=False,
        uploaded_at=_FixedDatetime(100),
    )
    only_default = _row(
        id_="donly", tags=["waiter"], is_default=True,
        uploaded_at=_FixedDatetime(200),
    )
    db = _fake_async_session([only_default, tagged])
    job = SimpleNamespace(title="Senior ML Engineer")

    result = await pick_resume_for_job(db, job)
    assert result is not None
    assert result.name == "tagged.pdf"


@pytest.mark.asyncio
async def test_default_beats_tagged_when_default_matches() -> None:
    """When both match the role, the default wins (canonical-bias on overlap ties)."""
    tagged = _row(
        id_="tagged", tags=["backend"], is_default=False,
        uploaded_at=_FixedDatetime(200),
    )
    default = _row(
        id_="def", tags=["backend"], is_default=True,
        uploaded_at=_FixedDatetime(100),
    )
    db = _fake_async_session([tagged, default])
    job = SimpleNamespace(title="Backend Engineer")

    result = await pick_resume_for_job(db, job)
    assert result is not None
    assert result.name == "def.pdf"


@pytest.mark.asyncio
async def test_more_recent_wins_on_identical_score() -> None:
    a = _row(
        id_="a", tags=["backend"], is_default=False,
        uploaded_at=_FixedDatetime(100),
    )
    b = _row(
        id_="b", tags=["backend"], is_default=False,
        uploaded_at=_FixedDatetime(200),
    )
    db = _fake_async_session([a, b])
    job = SimpleNamespace(title="Backend Engineer")

    result = await pick_resume_for_job(db, job)
    assert result is not None
    assert result.name == "b.pdf"


@pytest.mark.asyncio
async def test_target_roles_extend_match_surface() -> None:
    """Job.title might miss the user-configured role keywords; Settings fills in."""
    db = _fake_async_session([_row(id_="r1", tags=["frontend"], is_default=False)])
    job = SimpleNamespace(title="Software Developer")

    # No target_roles yet → no overlap.
    result = await pick_resume_for_job(db, job)
    assert result is not None
    assert result.name == "r1.pdf"

    # Now include target_roles=['frontend developer'] → overlap matches.
    db.get.return_value = SimpleNamespace(
        id=Preferences.SINGLETON_ID, target_roles=["frontend developer"],
    )
    result = await pick_resume_for_job(db, job)
    assert result is not None
    # Same single resume → still picked.
    assert result.name == "r1.pdf"


@pytest.mark.asyncio
async def test_singular_plural_match_via_naive_stem() -> None:
    """`engineers` (in job title) should match `engineer` (tag after stem)."""
    a = _row(id_="a", tags=["engineer"], is_default=False,
               uploaded_at=_FixedDatetime(100))
    b = _row(id_="b", tags=["frontend"], is_default=False,
               uploaded_at=_FixedDatetime(200))
    db = _fake_async_session([b, a])
    job = SimpleNamespace(title="Software Engineers")

    result = await pick_resume_for_job(db, job)
    assert result is not None
    assert result.name == "a.pdf"


@pytest.mark.asyncio
async def test_call_failure_does_not_crash_selector() -> None:
    """If the DB hiccups while loading Settings, the selector still picks."""
    db = _fake_async_session([_row(id_="r1", tags=["backend"], is_default=True)])
    db.get.side_effect = RuntimeError("db drop-out")
    job = SimpleNamespace(title="Backend Engineer")

    result = await pick_resume_for_job(db, job)
    assert result is not None
    assert result.name == "r1.pdf"
