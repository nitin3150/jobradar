"""Smoke tests for the alembic migration runner.

The runner is the bridge between ``utils.logging.jobradar_lifespan``
and alembic's programmatic API. Two things must hold for the
integration to be correct:

1. The module is importable from any CWD (pytest runs from the
   repo root, not from ``backend/``) so the lifespan can pull
   ``run_migrations_to_head`` without import errors.
2. ``BACKEND_DIR`` resolves to the directory that *actually*
   contains ``alembic.ini`` — otherwise the Config object would
   silently pick up a stale or missing ini and the migration
   would either no-op or raise a confusing
   ``alembic.util.exc.CommandError`` far from the root cause.

We do NOT exercise the live upgrade here. The ``conftest.py``
sets ``JOBRADAR_SKIP_MIGRATIONS=1`` so the lifespan does not run
migrations on every test, and the live path is already covered by
``tests/test_e2e_scan_to_jobs.py``'s real-Postgres fixtures. A
true "idempotent against a DB at head" test would require booting
the lifespan with the flag disabled, which would mutate the test
DB state for the rest of the suite — out of scope for a smoke
test.
"""
from __future__ import annotations

from db.migrations.runner import BACKEND_DIR, run_migrations_to_head


def test_runner_module_imports() -> None:
    """The public function is importable from any CWD.

    ``from db.migrations.runner import run_migrations_to_head`` is
    the line the lifespan uses; if pytest's import collection can
    resolve it without raising, the lifespan can too.
    """
    assert callable(run_migrations_to_head)


def test_backend_dir_resolves_to_alembic_ini() -> None:
    """``BACKEND_DIR`` is the directory that owns ``alembic.ini``.

    Catches the classic bug where someone moves ``runner.py`` to a
    different package and forgets to update the ``.parent.parent.parent``
    chain — the Config would then point at a directory without an
    ini, and alembic would fail with a confusing "alembic.ini not
    found" instead of the real "we moved the file" error.
    """
    assert (BACKEND_DIR / "alembic.ini").is_file(), (
        f"BACKEND_DIR ({BACKEND_DIR}) does not contain alembic.ini; "
        "check the .parent chain in db/migrations/runner.py"
    )


def test_runner_function_has_no_required_args() -> None:
    """The function takes no arguments — the lifespan calls it bare.

    Guards against an accidental signature change (e.g. someone
    adding a ``revision: str = "head"`` parameter). The lifespan
    pattern is ``await asyncio.to_thread(run_migrations_to_head)``;
    adding a required arg would break the lifespan at the first
    boot, which is the exact failure mode this module exists to
    prevent.
    """
    import inspect

    sig = inspect.signature(run_migrations_to_head)
    required = [
        name
        for name, param in sig.parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind is not inspect.Parameter.VAR_POSITIONAL
        and param.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    assert required == [], (
        f"run_migrations_to_head should take no required args, has: {required}"
    )
