"""Async Alembic environment — JobRadar persistence layer.

Alembic's default ``env.py`` is sync-only. FastAPI/asyncpg projects need a
slightly different shape: the connection is opened against an
``AsyncEngine``, the migration is dispatched via ``connection.run_sync``,
and the URL is sourced from ``DATABASE_URL`` so the docker-compose
deployment injects it uniformly.

Why async at all
================
The :mod:`backend.db.models` declarative ``Base`` is shared with the
FastAPI app (out of scope for v1 schema work, but planned). Mixing an
async migration runner with a sync runtime is a footgun — every pragma
the migration runs against ``connection`` has to be the sync dialect. We
build the async engine once, then convert to a sync connection at the
DML/DDL boundary.

Why we override ``sqlalchemy.url`` here
=======================================
``alembic.ini`` is checked into the repo with a fake URL placeholder so
operators don't accidentally check in a real connection string. This
file is the single point that translates ``DATABASE_URL`` from a
``postgresql+asyncpg://`` URL into the dialect Alembic needs.

Supabase pooler quirks
======================
When ``DATABASE_URL`` points at Supabase's **transaction-mode pooler**
(port 6543, host ``*.pooler.supabase.com``) the connection is routed
through pgBouncer, which **does not support prepared statements
across transaction boundaries**. asyncpg's default cache of
~100 prepared statements fails fast on a rotation-friendly pooler.

The fix is to set ``prepared_statement_cache_size=0`` on the URL so
SQLAlchemy turns it into an asyncpg connect kwarg disabling prepared
statements entirely. This is the configuration Supabase documents for
transaction-mode poolers and works without code changes on the direct
connection too (queries just execute every time, slower by ~5 % — fine
for Alembic which runs once per upgrade).

Reference: https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic
"""
from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ``prepend_sys_path = .`` in alembic.ini already inserts the backend
# directory (the one containing alembic.ini) at the front of sys.path,
# so a plain ``import db.models`` resolves without any extra work.
#
# A redundant filesystem guard below catches the rare case where someone
# runs ``alembic -c /some/abs/path/alembic.ini`` from a different
# working directory — without it the import silently fails with a
# confusing ModuleNotFoundError.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Mirror ``backend/main.py``'s env-precedence loader so `alembic upgrade`
# behaves identically to `uvicorn main:app` w.r.t. which DATABASE_URL it
# sees. Without this, operators who only set DATABASE_URL in
# ``backend/.env`` or repo-root ``.env`` (and not in their shell) get the
# sentinel ``driver://…`` fallback and a confusing NoSuchModuleError.
#
# Precedence (highest first): shell > backend/.env > repo-root/.env.
# Both files are loaded with ``override=False`` so process env always
# wins — docker-compose / shell-injected values stay authoritative.
_BACKEND_ENV = _BACKEND_DIR / ".env"
_ROOT_ENV = _BACKEND_DIR.parent / ".env"
if _BACKEND_ENV.is_file():
    load_dotenv(_BACKEND_ENV, override=False)
if _ROOT_ENV.is_file():
    load_dotenv(_ROOT_ENV, override=False)

from db.models import Base  # noqa: E402  (import after sys.path tweak + dotenv load)

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for ``autogenerate`` — Alembic compares this against the live
# database on ``alembic revision --autogenerate`` to diff new columns.
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# URL resolution — every Supabase-aware tweak passes through here so the
# engine builder (online) and the offline emitter use the same string.
# ---------------------------------------------------------------------------
_POOLER_MARKERS = ("pooler.supabase.com", ":6543")


def _resolve_database_url(*, require: bool = True) -> str:
    """Pull the asyncpg connection string from ``DATABASE_URL``.

    Applies three transformations once a URL has been resolved, and
    short-circuits to a clear ``RuntimeError`` if ``require=True`` and
    no URL is in the environment after the dotenv loader ran.

    1. **Driver upgrade.** A plain ``postgresql://`` URL is rewritten to
       ``postgresql+asyncpg://`` so docker compose variations that forget
       the driver prefix still work.
    2. **Pooler detection.** When the host contains ``pooler.supabase.com``
       or the port is ``6543``, append
       ``prepared_statement_cache_size=0`` so pgBouncer doesn't break on
       asyncpg's prepared-statement cache.
    3. **Missing-URL guard.** When ``DATABASE_URL`` is unset (no shell
       export, no ``backend/.env``, no repo-root ``.env``) and
       ``require=True`` — the path used by the online migrator —
       raise a readable ``RuntimeError`` instead of returning the
       sentinel ``"driver://…"`` string. Without this guard,
       SQLAlchemy mis-parses the sentinel as ``sqlalchemy.dialects:driver``
       and surfaces a cryptic ``NoSuchModuleError`` that buries the
       real cause ("DATABASE_URL is not set"). The offline
       ``alembic upgrade --sql`` emitter passes ``require=False`` so
       its DDL-preview output still works without an env file
       present.

    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        if require:
            raise RuntimeError(
                "DATABASE_URL is not set. Either export it in your shell "
                "(`export DATABASE_URL=postgresql+asyncpg://...`) or "
                "place it in backend/.env or repo-root/.env before "
                "running `alembic upgrade`. The dotenv loader above "
                "should already have picked up the .env file \u2014 if "
                "you see this error, the .env file is missing a "
                "DATABASE_URL line OR alembic was invoked from a "
                "directory where alembic's own .env-loader hasn't seen "
                "it (check `alembic current` first, then re-check the "
                "env vars). See README \u00a7Supabase Setup for the full "
                "Supabase-pooler URL pattern."
            )
        # Offline `alembic upgrade --sql` never opens a connection; this
        # sentinel is just enough for context.configure to identify a
        # Postgres dialect.
        return "driver://user:pass@host/dbname"

    # ── Driver upgrade ──────────────────────────────────────────────
    if url.startswith("postgresql://") and not url.startswith(
        "postgresql+asyncpg://"
    ):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    # ── Pooler detection + prepared-statement disable ───────────────
    if any(marker in url for marker in _POOLER_MARKERS):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if "prepared_statement_cache_size" not in query:
            query["prepared_statement_cache_size"] = "0"
        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout — used by ``alembic upgrade head --sql``.

    Doesn't actually need an async engine since it never opens a
    connection; just generates DDL. Kept here so ``alembic upgrade
    --sql`` "just works" for ops. ``require=False`` lets the offline
    emitter fall back on the sentinel URL when DATABASE_URL is unset
    (the operator may not have a real DB handy but still wants to
    preview the DDL diff).
    """
    context.configure(
        url=_resolve_database_url(require=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Sync callback that ``connection.run_sync`` invokes on an async conn."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Compare types so column-type changes (e.g., ``String(50)`` →
        # ``String(120)``) are picked up by autogenerate.
        compare_type=True,
        # Compare server defaults so added ``server_default`` flags show up.
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Open an ``AsyncEngine`` and dispatch ``do_run_migrations`` on the conn."""
    section = config.get_section(config.config_ini_section) or {}
    # ``require=True`` (the default): missing DATABASE_URL falls through
    # to a clear ``RuntimeError`` instead of SQLAlchemy's cryptic
    # ``NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:driver``.
    section["sqlalchemy.url"] = _resolve_database_url(require=True)
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
