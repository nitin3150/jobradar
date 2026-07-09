"""Async SQLAlchemy engine + session factory for JobRadar Postgres I/O.

Why this module exists
======================

The previous shape had every route wired to an in-memory ``_FOO_DB``
dict. After migrating the four read/write touchpoints of
``backend/routes/jobs.py`` and the ``score_and_persist`` write path
in :mod:`services.scoring_service` to real Postgres, we need:

* **one** lazily-initialised async engine per process (the pooler/Supavisor
  side already does connection pooling, so a single ``create_async_engine``
  in this module is enough);
* **one** sessionmaker whose sessions are cheap to construct inside a
  request or scoring call;
* a FastAPI dependency factory ``get_session`` that yields a session
  and commits-or-rollbacks once the route returns.

Configuration
=============

``DATABASE_URL`` is read once at module import — it must be present in
the process environment by the time :func:`get_session` is first
called. The URL must point at an ``asyncpg``-compatible DSN
(``postgresql+asyncpg://…``) — alembic auto-generates DDL with the same
driver so dev/CI parity is preserved.

Module-init guard
=================

If ``DATABASE_URL`` is unset when this module is imported, we set a
flag instead of crashing at import — :func:`get_session` will raise
:class:`RuntimeError` with a clear remediation message on first use.
That matches the deferral pattern in :mod:`storage.supabase` so an
operator can still boot the rest of the routes (e.g. ``/api/scan/<d>``
which today doesn't touch the DB at all) without configuring a DB.

Why ``expire_on_commit=False``
==============================

By default SQLAlchemy expires every loaded attribute after ``commit()``
so subsequent access re-issues a SELECT. We disable that because the
route handlers convert ORM rows into Pydantic ``Job`` models *inside
the request* — re-issuing Selects on every attribute access after
commit would be wasteful and would also break the pattern of
constructing the response and returning from the handler in one shot.
Pydantic reads attributes once at model-validation time, so the
non-expiring behaviour is both faster and required for our shape.
"""
from __future__ import annotations

import logging
import os
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine import URL

logger = logging.getLogger("jobradar.db")


# ----------------------------------------------------------------------
# Lazy engine construction. ``DATABASE_URL`` is read here at module import
# time — same precedence rules as the FastAPI lifespan in main.py
# (process env > backend/.env > repo-root/.env).
# ----------------------------------------------------------------------
_DATABASE_URL: str | None = os.environ.get("DATABASE_URL", "").strip() or None


_engine = None
if _DATABASE_URL:
    # ``JOBRADAR_TEST_DB=1`` opts the process into NullPool so every
    # ``AsyncSessionLocal()`` opens + closes its own connection bound
    # to the calling event loop. This is the right shape for the
    # ``unittest.IsolatedAsyncioTestCase`` test runner — each test
    # gets a fresh event loop, and a pooled connection that was bound
    # to a previous test's closed loop raises
    # ``RuntimeError: Task ... attached to a different loop``. The
    # Supabase pooler still pools at the TCP layer, so production
    # behaviour is unaffected when this flag is unset.
    if os.environ.get("JOBRADAR_TEST_DB", "").strip() in ("1", "true"):
        from sqlalchemy.pool import NullPool

        _engine = create_async_engine(
            _DATABASE_URL,
            poolclass=NullPool,
            echo=False,
        )
    else:
        # Production path: cheap ``pool_pre_ping`` insurance against
        # the Supabase pooler recycling a TCP connection asyncpg hasn't
        # noticed yet.
        _engine = create_async_engine(
            _DATABASE_URL,
            pool_pre_ping=True,
            echo=False,
        )

# Public default so callers that don't go through FastAPI's
# ``Depends(get_session)`` (e.g. ``scoring_service.score_and_persist``
# which constructs its own session inside ``asyncio.run``) can still
# share the same pool.
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    if _engine is not None
    else None
)


# ----------------------------------------------------------------------
# Public helpers
# ----------------------------------------------------------------------
def require_database_configured() -> None:
    """Raise a clear remediation error when DB env is missing.

    Called by :func:`get_session` and by ``scoring_service`` so the
    operator sees the same message regardless of which read/write
    path is the first to discover the mis-config.
    """
    if _engine is None or AsyncSessionLocal is None:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it in the environment "
            "(see backend/.env.example for the canonical pattern) and "
            "restart the process. The URL must include the +asyncpg "
            "driver suffix, e.g. "
            "postgresql+asyncpg://postgres.<project>:<password>"
            "@aws-1-us-west-2.pooler.supabase.com:6543/postgres"
        )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency factory — yields an :class:`AsyncSession`.

    Commit/rollback discipline lives inside the route handler so the
    dependency itself stays simple. We close the session on exit so
    the underlying connection is returned to the pool no matter what.
    """
    require_database_configured()
    assert AsyncSessionLocal is not None  # noqa: S101  (assert for type checker)
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


__all__ = ["AsyncSessionLocal", "get_session", "require_database_configured"]
