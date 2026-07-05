import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from app.resumes.profile import compose_profile


def test_compose_with_resume_and_roles():
    out = compose_profile("Nitin — LangGraph, FastAPI", ["AI Engineer", "ML Engineer"])
    assert "AI Engineer" in out and "ML Engineer" in out
    assert "LangGraph" in out


def test_compose_roles_only():
    out = compose_profile(None, ["LLM Engineer"])
    assert "LLM Engineer" in out


def test_compose_resume_only():
    out = compose_profile("PyTorch, Docker", [])
    assert "PyTorch" in out


def test_compose_empty_returns_blank():
    # No resume, no roles -> "" so the scorer applies its own default fallback.
    assert compose_profile(None, []) == ""
    assert compose_profile("", []) == ""


@pytest.mark.asyncio
async def test_build_uses_passed_prefs_without_refetch():
    from app.resumes.profile import build_candidate_profile

    db = AsyncMock()
    # No default resume row.
    exec_result = MagicMock()
    exec_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=exec_result)
    db.get = AsyncMock()  # must NOT be called when prefs is provided

    prefs = SimpleNamespace(target_roles=["AI Engineer"])
    out = await build_candidate_profile(db, prefs=prefs)

    assert "AI Engineer" in out
    db.get.assert_not_called()
