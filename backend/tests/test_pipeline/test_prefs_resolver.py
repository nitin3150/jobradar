from types import SimpleNamespace

from app.pipeline.jobs import resolve_prefs


def _defaults():
    return SimpleNamespace(
        target_roles=["Software Engineer"],
        job_fit_threshold=0.6,
        review_window_hours=2,
    )


def test_resolve_uses_db_prefs_when_present():
    prefs = SimpleNamespace(
        target_roles=["AI Engineer"], job_fit_threshold=0.8, review_window_hours=3.0
    )
    roles, threshold, window = resolve_prefs(prefs, _defaults())
    assert roles == ["AI Engineer"]
    assert threshold == 0.8
    assert window == 3.0


def test_resolve_falls_back_to_defaults_when_no_row():
    roles, threshold, window = resolve_prefs(None, _defaults())
    assert roles == ["Software Engineer"]
    assert threshold == 0.6
    assert window == 2


def test_resolve_empty_db_roles_fall_back_to_defaults():
    prefs = SimpleNamespace(
        target_roles=[], job_fit_threshold=0.7, review_window_hours=1.0
    )
    roles, threshold, window = resolve_prefs(prefs, _defaults())
    # Empty target_roles in DB should not blank out the prefilter.
    assert roles == ["Software Engineer"]
    assert threshold == 0.7
