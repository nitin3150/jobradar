import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/api/settings",
        "/api/resumes",
        "/api/jobs",
        "/api/applications",
        "/api/qa-bank",
        "/api/outreach/generate",
    ],
)
def test_router_mounted(path):
    from app.main import app

    paths = list(app.openapi().get("paths", {}).keys())
    assert any(p == path or p.startswith(path) for p in paths), (path, paths)
