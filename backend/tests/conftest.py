"""Pytest configuration + shared async fixtures for the backend test suite.

Why this file exists
====================

The previous test pattern (:mod:`test_jobs`, :mod:`test_applications`,
:mod:`test_e2e_scan_to_jobs`) used ``unittest.TestCase`` with a sync
``setUp`` that called ``asyncio.run(_seed_job_rows(_session()))`` to
populate the DB. That worked when pytest-asyncio was not installed,
but the moment pytest-asyncio 1.4.0 is on the path it creates a
session-scoped event loop, and ``asyncio.run()`` from inside that
thread raises ``RuntimeError: asyncio.run() cannot be called from a
running event loop``.

This conftest replaces the bridge with the canonical pytest-asyncio
shape:

* async fixtures for setup / teardown (no more ``asyncio.run()``);
* ``httpx.AsyncClient`` + ``ASGITransport`` for the route calls
  (was ``fastapi.testclient.TestClient``);
* ``[tool.pytest.ini_options]`` in :mod:`pyproject.toml` sets
  ``asyncio_mode = "auto"`` so every ``async def test_*`` and
  every ``@pytest_asyncio.fixture`` is recognized without
  per-test ``@pytest.mark.asyncio`` decoration.

Why function-scoped fixtures
============================

:mod:`db.session` already opts into ``NullPool`` when
``JOBRADAR_TEST_DB=1`` is set, so every ``AsyncSessionLocal()``
opens + closes its own connection bound to the calling event
loop. Pairing NullPool with function-scoped pytest fixtures gives
each test a fresh loop + fresh connection — no cross-test
``Task ... attached to a different loop`` errors.
"""
from __future__ import annotations

import os
from typing import AsyncIterator

# Set BEFORE any other import. ``db.session`` reads ``DATABASE_URL`` +
# ``JOBRADAR_TEST_DB`` at module-import time and constructs the engine
# with NullPool if the flag is set. Setting it here means a plain
# ``pytest`` invocation (no explicit ``JOBRADAR_TEST_DB=1`` env var
# prefix) still gets the right test engine. ``setdefault`` is a
# no-op if the operator already exported the flag, so production
# tests that pin a specific value are unaffected.
os.environ.setdefault("JOBRADAR_TEST_DB", "1")

# ``JOBRADAR_SKIP_MIGRATIONS=1`` opts the test runner out of the
# auto-migration-on-startup that ``utils.logging.jobradar_lifespan``
# wires into every FastAPI boot. The lifespan fires on every
# ``TestClient(app)`` (or in this repo, every ``httpx.AsyncClient`` +
# ``ASGITransport(app=app)``) construction, so without this escape
# hatch every test would pay the alembic-upgrade cost AND race the
# test fixtures' own seed helpers for the same rows. The conftest
# fixtures already bring the schema to a known state via
# ``_seed_job_rows`` / ``_seed_applications`` — re-running alembic on
# top of that is wasteful. Set the flag here at the very top of the
# file so the ``from main import app`` import below picks it up.
os.environ.setdefault("JOBRADAR_SKIP_MIGRATIONS", "1")

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete

from db import models as db_models
from db.session import AsyncSessionLocal
from main import app
from routes.applications import _seed_applications
from routes.jobs import _seed_job_rows
from routes.settings import _PREFS_STATE, _reset_prefs


# ----------------------------------------------------------------------
# ASGI client
# ----------------------------------------------------------------------
@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """In-process ASGI client.

    ``base_url='http://test'`` is a placeholder so relative paths
    like ``client.get('/api/jobs')`` resolve cleanly — the transport
    is in-process (no real socket, no port collisions), so the host
    portion is never actually dialed.

    Function-scoped (the pytest-asyncio default in ``auto`` mode)
    so a test that mutates app state via dependency-injection
    overrides doesn't leak into the next test.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ----------------------------------------------------------------------
# Per-table seed fixtures
# ----------------------------------------------------------------------
@pytest_asyncio.fixture
async def seeded_jobs(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """Truncate + reseed the ``jobs`` table to the canonical 6 fixture
    rows, then yield the ASGI client.

    Teardown wipes the table so the next test starts clean —
    matches the previous ``_JobsTestCase.tearDown`` contract so
    scoring-service writes from one test never bleed into the next.

    Tests that need the seeded ``jobs`` table + the FastAPI app
    client take this single fixture. Tests that need the client
    but don't care about seeded jobs take ``client`` directly.
    """
    await _seed_job_rows(AsyncSessionLocal())
    try:
        yield client
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(db_models.Job))
            await session.commit()


@pytest_asyncio.fixture
async def seeded_applications(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """Truncate + reseed the ``applications`` table to the canonical
    6 fixture rows, then yield the client.

    Used by the list / PATCH-status tests in :mod:`test_applications`.
    The POST tests need both tables seeded — they take
    :func:`seeded_jobs_and_applications` instead.
    """
    await _seed_applications(AsyncSessionLocal())
    try:
        yield client
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(db_models.Application))
            await session.commit()


@pytest_asyncio.fixture
async def seeded_jobs_and_applications(
    client: AsyncClient,
) -> AsyncIterator[AsyncClient]:
    """Seed both ``jobs`` and ``applications`` to their canonical
    6-row fixtures.

    Used by the ``POST /api/applications`` tests in
    :mod:`test_applications` which need a real Job row for the
    FK lookup + a real Application table for the list-after-POST
    assertions. The previous test class composed this via
    ``class TestCreateApplicationFromJob(_JobSeedMixin, _ApplicationsTestCase)``
    which is impossible to express cleanly in pytest — a fixture
    composition is the canonical replacement.

    Teardown wipes both tables in a single transaction so the
    next test starts with a clean state.
    """
    await _seed_applications(AsyncSessionLocal())
    await _seed_job_rows(AsyncSessionLocal())
    try:
        yield client
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(db_models.Application))
            await session.execute(sa_delete(db_models.Job))
            await session.commit()


# ----------------------------------------------------------------------
# Misc fixtures
# ----------------------------------------------------------------------
@pytest_asyncio.fixture
async def reset_prefs() -> AsyncIterator[None]:
    """Reset ``routes.settings._PREFS_STATE`` to factory defaults
    (``job_fit_threshold = 0.6``).

    Used by the scan tests (:mod:`test_e2e_scan_to_jobs`) whose
    mock LLM scores (0.85 / 0.91 / 0.30) are calibrated against
    the default 0.6 threshold. ``_reset_prefs`` is a sync function
    (it mutates a module-level dict); calling it from an async
    fixture is fine because the body is pure-Python with no
    ``await`` points.

    The post-yield body is intentionally a no-op: ``_reset_prefs``
    is idempotent and the next test that takes this fixture will
    reset again. The in-memory dict is process-local and dies
    with the worker, so no test-to-test leak is possible.
    """
    _reset_prefs()
    # Sanity-pin: the post-reset threshold is the value the scan
    # tests' mock scores are calibrated against. A future refactor
    # of the default surfaces here as an immediate ``AssertionError``
    # on the very next test collection, before any test body runs.
    assert _PREFS_STATE["data"]["job_fit_threshold"] == 0.6
    yield
