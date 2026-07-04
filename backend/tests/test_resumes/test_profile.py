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
