"""Programmatic alembic migration runner for the FastAPI lifespan.

Why this file exists
====================

Before v0.5 the only path that applied schema changes was a manual
``cd backend && alembic upgrade head`` from an operator shell. That
worked locally but the Render deployment was missing the same
discipline: the FastAPI service would boot, start serving requests
against the *previous* schema, and ``GET /api/jobs`` would 500 with
``column jobs.posted_at does not exist`` the moment a model was
shipped ahead of its migration.

This module provides a single importable function,
:func:`run_migrations_to_head`, that wires alembic's programmatic
API into a sync callable. It is called from two places today:

1. ``utils.logging.jobradar_lifespan`` — the FastAPI
   ``@asynccontextmanager`` that runs on every process boot. Wrapped
   in ``asyncio.to_thread`` so the lifespan's event loop is not
   blocked. This is the path that protects the Render service in the
   common case (operator pushes code, Render builds + restarts, app
   boots, lifespan runs the migration, *then* starts serving
   requests).
2. ``render.yaml::services[0].preDeployCommand`` — runs once per
   Render deploy *before* the new container is swapped in. Belt-and-
   suspenders with the lifespan path: preDeployCommand catches the
   migration before health checks fire; the lifespan catches any
   drift if a future deploy ever forgets the preDeployCommand.

Idempotency
===========

``alembic upgrade head`` against a database already at head is a
no-op (Alembic reads ``alembic_version`` and bails early). Running
this function from both preDeployCommand and the lifespan is safe:
if preDeployCommand already upgraded, the lifespan call sees head
and logs "no upgrade needed".

Failure semantics
=================

A migration failure must crash the boot. Two reasons:

* If the lifespan swallows the exception and starts serving requests
  anyway, the very next request that touches the missing column
  500s — exactly the bug this module exists to prevent.
* If ``preDeployCommand`` exits non-zero, Render aborts the deploy
  and keeps the previous live container serving traffic. Raising
  from this function is what surfaces the failure to Render.

This module does not catch exceptions; the caller is expected to
let them propagate. The lifespan just ``await``s the call directly
— Starlette's lifespan machinery already logs uncaught exceptions
with a full traceback, so wrapping the call in a
``try / except / raise`` would only duplicate the log line.

Reference: https://alembic.sqlalchemy.org/en/latest/api/commands.html
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command as _alembic_command
from alembic.config import Config as _AlembicConfig


# Public so ``tests/test_migration_runner.py`` can assert the path
# resolution. Resolves ``backend/`` (the directory that contains
# ``alembic.ini``) from this file's location with a three-step
# ``.parent`` walk. The same path is used as uvicorn's CWD when
# ``main.py`` is invoked, so alembic's ``prepend_sys_path = .``
# setting resolves the ``db/`` package import.
BACKEND_DIR: Final[Path] = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI: Final[Path] = BACKEND_DIR / "alembic.ini"


def _build_alembic_config() -> _AlembicConfig:
    """Build a fresh :class:`alembic.config.Config` pointed at the
    backend's ``alembic.ini``.

    A new Config is constructed on every call (rather than cached
    at module import) because Alembic reads ``DATABASE_URL`` from
    the process environment at *command* time via ``env.py``. A
    cached config would miss env edits made between
    :func:`main._load_env_files` and the call to
    :func:`run_migrations_to_head` in test scenarios that mutate the
    env between those two points.
    """
    return _AlembicConfig(str(ALEMBIC_INI))


def run_migrations_to_head() -> None:
    """Apply pending alembic migrations to ``head``. Idempotent.

    No-op when the database is already at head — alembic's own
    ``upgrade`` short-circuits after reading ``alembic_version``,
    so calling this function on every process boot is safe.

    Raises
    ------
    sqlalchemy.exc.OperationalError
        When the database is unreachable. Boot must fail.
    alembic.util.exc.CommandError
        When a migration script raises (bad DDL, FK violation,
        etc.). Same propagation rule.
    FileNotFoundError
        When ``alembic.ini`` is missing (misdirected import).
    """
    cfg = _build_alembic_config()
    _alembic_command.upgrade(cfg, "head")


__all__ = ["run_migrations_to_head", "BACKEND_DIR", "ALEMBIC_INI"]
