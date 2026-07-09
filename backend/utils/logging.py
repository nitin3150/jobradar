"""Centralised access + error logging for the JobRadar backend.

The module is intentionally side-effect-light: nothing installs handlers
or middleware at import time; everything happens via the public
helpers below, called from ``main.py`` (production) or directly from
tests. The four primitives are:

- :func:`setup_logging` — idempotently configure the root logger format
  + level. Honours the ``LOG_LEVEL`` env var so operators can dial it
  up (DEBUG) or down (WARNING) without restarting. Safe to call multiple
  times (used by the lifespan *and* the test suite).
- :func:`jobradar_lifespan` — ``@asynccontextmanager`` lifespan that
  calls ``setup_logging()`` and :func:`dump_routes` on startup. Wired
  into the FastAPI app via ``FastAPI(lifespan=...)`` in ``main.py``;
  ``TestClient`` exercises the same path so the access log is always
  configured before any test request fires.
- :func:`dump_routes` — print every mounted HTTP route at startup so
  the operator can see what's actually reachable (the recent
  404-cluster on ``/api/resumes`` / ``/api/settings`` /
  ``/api/jobs/pending-count`` is debuggable exactly because nothing is
  mounted under those prefixes — this makes that obvious).
- :class:`RequestLoggingMiddleware` — per-request access log. Times
  every request with ``time.monotonic``, attaches an 8-char
  ``X-Request-ID`` (uuid4 hex), exposes ``request.state.request_id``
  so the global exception handler can echo it back in 5xx bodies, and
  logs uncaught exceptions with a stack trace before re-raising.

Tests in ``tests/test_request_logging.py`` pin:

- ``X-Request-ID`` is on every response and varies between calls
- the access log records method / path / status / duration
- 404 responses are logged at INFO so the recent 404 cluster is
  easy to grep for
- ``setup_logging`` is idempotent (calling twice does not stack handlers)
- ``dump_routes`` emits one line per (method, path) pair the app has

The middleware uses :class:`starlette.middleware.base.BaseHTTPMiddleware`;
that class has a known edge case with streaming responses (it buffers
the body, which can hurt long-lived ``StreamingResponse`` outputs),
but every route in this service returns JSON via FastAPI's default
``JSONResponse`` so the buffer-mismatch never triggers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from db.migrations.runner import run_migrations_to_head
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request


# ---------------------------------------------------------------------------
# Module-level loggers. Importers use these directly so tests can attach
# capture handlers without reaching into private state.
# ---------------------------------------------------------------------------
_access_log = logging.getLogger("jobradar.request")
_startup_log = logging.getLogger("jobradar.startup")
_error_log = logging.getLogger("jobradar.error")


DEFAULT_FORMAT = "%(asctime)sZ %(levelname)-5s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_configured = False


def setup_logging(level: int | None = None) -> None:
    """Configure the root logger exactly once.

    The format is ISO-8601 + level + logger name + message. A single
    ``StreamHandler`` is attached (uvicorn's own handlers are kept).
    Idempotent so test ``setUp`` calls don't stack handlers across
    modules.

    ``level`` is taken from the ``LOG_LEVEL`` env var when omitted
    (defaults to ``INFO``).
    """
    global _configured
    if _configured:
        return
    if level is None:
        env = os.environ.get("LOG_LEVEL", "").strip().upper()
        level = _LEVELS.get(env, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=DEFAULT_FORMAT, datefmt=_DATEFMT))
    root = logging.getLogger()
    # Replace handlers — pytest captures its own handlers, and stacking
    # ours on top causes double output. Tests that want to inspect
    # records attach their own capture handler after calling
    # ``setup_logging``.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def reset_logging_for_tests() -> None:
    """Test seam — drops the configured-flag so a fresh ``setup_logging``
    can be issued with a different level.

    Not used in production code. Imported by ``tests/test_request_logging``
    to reset between test classes that need a different ``LOG_LEVEL``.
    """
    global _configured
    _configured = False


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def _format_request_line(request: "Request", status: int, duration_s: float) -> str:
    qs = request.url.query
    full_path = f"{request.url.path}?{qs}" if qs else request.url.path
    client = f"{request.client.host}:{request.client.port}" if request.client else "?"
    return (
        f"req_id={getattr(request.state, 'request_id', '-')} "
        f"{request.method} {full_path} -> {status} "
        f"({duration_s:.3f}s) client={client}"
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Per-request access log + ``X-Request-ID`` injector.

    Order in FastAPI: the middleware added *last* runs first on the
    request side, so adding this *after* ``CORSMiddleware`` makes the
    access log wrap CORS — every response (including preflight
    rejections and downstream 4xx) is recorded once with a single
    request ID.
    """

    def __init__(self, app, *, logger: logging.Logger | None = None) -> None:
        super().__init__(app)
        self._log = logger if logger is not None else _access_log

    async def dispatch(self, request, call_next):  # type: ignore[override]
        request.state.request_id = new_request_id()
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.monotonic() - start
            self._log.exception(
                "UNCAUGHT %s",
                _format_request_line(request, 500, duration),
            )
            raise
        duration = time.monotonic() - start
        response.headers["X-Request-ID"] = request.state.request_id
        self._log.info(_format_request_line(request, response.status_code, duration))
        return response


def dump_routes(app: "FastAPI") -> None:
    """Log every mounted HTTP route at startup.

    Uses :func:`_iter_routes` — which reads ``app.openapi()`` as the
    source of truth, not ``app.routes`` — so the dump picks up paths
    contributed by ``include_router`` even when FastAPI wraps them in
    private ``_IncludedRouter`` objects that don't expose ``path`` /
    ``methods`` / ``routes``. Strips Starlette / FastAPI tooling
    (``/openapi.json``, ``/docs*``, ``/redoc``) so the operator-facing
    dump stays readable.
    """
    _startup_log.info("mounted routes (%d):", _route_count(app))
    for path, methods in _iter_routes(app):
        if _is_tooling_path(path):
            continue
        for method in sorted(methods - {"HEAD"}):
            _startup_log.info("  %-6s %s", method, path)


def _route_count(app: "FastAPI") -> int:
    return sum(
        len(methods - {"HEAD"})
        for path, methods in _iter_routes(app)
        if not _is_tooling_path(path)
    )


_TOOLING_PREFIXES = ("/openapi.json", "/docs", "/redoc")


def _is_tooling_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _TOOLING_PREFIXES)


def _iter_routes(app: "FastAPI"):
    """Yield ``(path, methods_set)`` for every APIRoute under ``app``.

    Implementation note: this helper reads ``app.openapi()["paths"]``
    rather than walking ``app.routes`` because :class:`fastapi.FastAPI`
    sometimes stores included routes in private ``_IncludedRouter``
    wrappers (no ``path``, no ``methods``, no ``routes`` attribute)
    that are invisible to a flat ``app.routes`` walk. The OpenAPI spec
    is the canonical, fully-prefixed path view — every mounted route
    appears with its full prefix regardless of the internal storage
    shape. ``HEAD`` mirrors that FastAPI auto-adds next to every
    ``GET`` are stripped here so callers don't double-count.
    """
    spec = app.openapi()
    seen: set[str] = set()
    for path, ops in spec["paths"].items():
        if path in seen:
            continue
        seen.add(path)
        yield path, {method.upper() for method in ops.keys()}


@asynccontextmanager
async def jobradar_lifespan(app: "FastAPI") -> AsyncIterator[None]:
    """Application lifespan: configure logging + run migrations + dump routes on startup.

    Used as ``FastAPI(lifespan=...)`` in ``main.py``. ``TestClient``
    runs the lifespan on construction, so the access log is configured
    and the route dump is written before any test request fires.

    Migration runner
    ----------------
    After logging is configured but before routes are dumped (and
    long before any request lands), every pending alembic migration
    is applied via :func:`db.migrations.runner.run_migrations_to_head`.
    This protects the Render deployment from the v0.5-class outage
    where the app boots against a stale schema and the very first
    request that touches a new column 500s with
    ``column jobs.posted_at does not exist``.

    The runner is sync, so it is offloaded to a thread via
    :func:`asyncio.to_thread` to avoid blocking the lifespan's
    event loop. Exceptions propagate so a failed migration
    crashes the boot — which is the correct behaviour on Render:
    the existing container keeps serving traffic, the new build
    is marked failed, and no 5xx ever lands on the wire.
    Starlette's lifespan machinery already logs uncaught exceptions
    with a full traceback, so we do not wrap the call in a redundant
    ``try / except / raise``.

    Set ``JOBRADAR_SKIP_MIGRATIONS=1`` (or ``true``) to opt out —
    matches the convention :mod:`db.session` uses for
    ``JOBRADAR_TEST_DB``. The test suite sets this in
    ``backend/tests/conftest.py`` because the lifespan fires on
    every ``AsyncClient(transport=ASGITransport(app=app))``
    construction — paying the migration cost (and writing to a
    real DB) on every test is unnecessary because the
    conftest-managed fixtures already bring the schema to a known
    state via their own seed helpers.
    """
    setup_logging()
    _startup_log.info("JobRadar backend starting up (LOG_LEVEL=%s)",
                      logging.getLevelName(logging.getLogger().level))
    if os.environ.get("JOBRADAR_SKIP_MIGRATIONS", "").strip() in ("1", "true"):
        _startup_log.info("alembic: skipped (JOBRADAR_SKIP_MIGRATIONS=1)")
    else:
        # ``asyncio.to_thread`` offloads the sync alembic call so the
        # lifespan's event loop is not blocked. An uncaught exception
        # here propagates out of the asynccontextmanager — Starlette
        # surfaces it as a failed lifespan start, which the FastAPI
        # / uvicorn boot path reports to the operator verbatim.
        await asyncio.to_thread(run_migrations_to_head)
    dump_routes(app)
    try:
        yield
    finally:
        _startup_log.info("JobRadar backend shutting down")
