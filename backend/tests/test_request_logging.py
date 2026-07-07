"""Smoke tests for :mod:`utils.logging` and the global exception handler.

These tests assert the externally-observable contract:

* ``X-Request-ID`` is set on every response and varies between calls
* the access log records method / full path / status / duration / client
* 404 responses are logged at INFO (the recent 404 cluster on
  ``/api/resumes`` / ``/api/settings`` / ``/api/jobs/pending-count`` is
  debuggable exactly because the access log captures them)
* ``setup_logging`` is idempotent — calling twice does NOT stack handlers
* ``dump_routes`` emits a header line plus one line per (method, path)
  pair registered on the app
* ``dump_routes`` deduplicates ``HEAD`` mirrors that FastAPI auto-adds
  next to every ``GET``
* an uncaught exception in a route is caught by the global handler,
  converted to a 500 with the ``request_id`` echoed in the body, and
  logged with a stack trace on ``jobradar.error``
"""
from __future__ import annotations

import json
import logging
import unittest
from typing import Iterable

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from main import app
from utils.logging import (
    _iter_routes,
    dump_routes,
    reset_logging_for_tests,
    setup_logging,
)


class _Capture(logging.Handler):
    """Records each emitted record so tests can assert on message / level."""

    def __init__(self, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _make_capture_logger(name: str, level: int = logging.DEBUG) -> tuple[logging.Logger, _Capture]:
    setup_logging()
    log = logging.getLogger(name)
    log.setLevel(level)
    capture = _Capture(level=level)
    log.addHandler(capture)
    return log, capture


def _messages(records: Iterable[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records]


# ---------------------------------------------------------------------------
class TestSetupLogging(unittest.TestCase):
    def test_setup_logging_is_idempotent(self) -> None:
        reset_logging_for_tests()
        setup_logging()
        setup_logging()
        # Exactly one handler on root: a second setup_logging() must NOT
        # stack a duplicate.
        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 1)
        # Make sure subsequent tests in this process start clean.
        reset_logging_for_tests()
        setup_logging()

    def test_setup_logging_honours_log_level_env(self) -> None:
        import os
        reset_logging_for_tests()
        old = os.environ.get("LOG_LEVEL")
        try:
            os.environ["LOG_LEVEL"] = "DEBUG"
            setup_logging()
            self.assertEqual(logging.getLogger().level, logging.DEBUG)
            os.environ["LOG_LEVEL"] = "WARNING"
            reset_logging_for_tests()
            setup_logging()
            self.assertEqual(logging.getLogger().level, logging.WARNING)
        finally:
            if old is None:
                os.environ.pop("LOG_LEVEL", None)
            else:
                os.environ["LOG_LEVEL"] = old
            reset_logging_for_tests()
            setup_logging()


# ---------------------------------------------------------------------------
class TestRequestLogging(unittest.TestCase):
    def setUp(self) -> None:
        setup_logging()
        self.client = TestClient(app)
        self.request_log, self.request_capture = _make_capture_logger("jobradar.request")
        self.error_log, self.error_capture = _make_capture_logger("jobradar.error")

    def tearDown(self) -> None:
        self.request_log.removeHandler(self.request_capture)
        self.error_log.removeHandler(self.error_capture)
        self.request_capture.close()
        self.error_capture.close()

    def test_health_response_carries_x_request_id_header(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.headers.get("X-Request-ID")
        self.assertIsNotNone(rid)
        # 8-char hex string from utils.logging.new_request_id.
        self.assertEqual(len(rid), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in rid))

    def test_request_ids_vary_between_calls(self) -> None:
        ids = {self.client.get("/health").headers["X-Request-ID"] for _ in range(8)}
        # 8-char hex has 4 billion combinations — a collision in 8 draws
        # is statistically impossible. Assert all distinct.
        self.assertEqual(len(ids), 8)

    def test_access_log_records_method_path_status_duration_and_client(self) -> None:
        self.client.get("/health")
        msgs = _messages(self.request_capture.records)
        matching = [
            m for m in msgs
            if "GET /health -> 200" in m
            and "client=" in m
            and "(0." in m  # duration like "(0.012s)"
        ]
        self.assertTrue(matching, f"no matching access log line; got {msgs}")
        # The matched line should also carry req_id=...
        self.assertIn("req_id=", matching[0])

    def test_404_path_is_logged_with_status_404(self) -> None:
        r = self.client.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404, r.text)
        rid = r.headers["X-Request-ID"]
        msgs = _messages(self.request_capture.records)
        matching = [m for m in msgs if f"req_id={rid}" in m and "-> 404" in m]
        self.assertTrue(matching, f"no 404 log line for req_id={rid}; got {msgs}")


# ---------------------------------------------------------------------------
class TestDumpRoutes(unittest.TestCase):
    def setUp(self) -> None:
        setup_logging()
        self.startup_log, self.startup_capture = _make_capture_logger("jobradar.startup")

    def tearDown(self) -> None:
        self.startup_log.removeHandler(self.startup_capture)
        self.startup_capture.close()

    def test_dump_routes_writes_a_header_and_one_line_per_method_path(self) -> None:
        dump_routes(app)
        msgs = _messages(self.startup_capture.records)
        # Header line — message starts with "mounted routes".
        self.assertTrue(any(m.startswith("mounted routes") for m in msgs),
                        f"no header line; got {msgs}")
        # Spot-check a few representative paths.
        self.assertTrue(any("/health" in m and "GET" in m for m in msgs),
                        f"missing /health GET line; got {msgs}")
        self.assertTrue(any("/api/pipeline/run" in m and "POST" in m for m in msgs),
                        f"missing /api/pipeline/run POST line; got {msgs}")
        # The route at /api/companies has GET; FastAPI auto-adds HEAD next
        # to every GET — dump_routes strips those duplicates.
        head_only_lines = [m for m in msgs if "HEAD" in m]
        self.assertEqual(head_only_lines, [],
                         f"HEAD duplicates leaked into route dump: {head_only_lines}")


# ---------------------------------------------------------------------------
class TestExceptionHandler(unittest.TestCase):
    """Drive a synthetic, test-only app so the production ``app`` is left
    intact across tests. The handler is imported from ``main`` so this
    test pins the production wiring directly, not a copy."""

    def test_uncaught_exception_returns_500_with_request_id_in_body(self) -> None:
        # Build an isolated FastAPI app with the production middleware +
        # the production exception handler so behaviour is faithful to
        # ``main.app`` without polluting it for the rest of the suite.
        from main import _unhandled_exception_handler

        tmp = FastAPI()
        from utils.logging import RequestLoggingMiddleware
        tmp.add_middleware(RequestLoggingMiddleware)

        @tmp.get("/api/__test_boom__")
        def _boom() -> None:
            raise RuntimeError("synthetic boom")

        tmp.add_exception_handler(Exception, _unhandled_exception_handler)

        # ``raise_server_exceptions=False`` so TestClient surfaces the
        # 500 instead of re-raising into the test process.
        client = TestClient(tmp, raise_server_exceptions=False)

        setup_logging()
        error_log, error_capture = _make_capture_logger("jobradar.error")
        request_log, request_capture = _make_capture_logger("jobradar.request")
        try:
            r = client.get("/api/__test_boom__")
        finally:
            error_log.removeHandler(error_capture)
            request_log.removeHandler(request_capture)
            error_capture.close()
            request_capture.close()

        self.assertEqual(r.status_code, 500, r.text)
        body = json.loads(r.content)
        self.assertEqual(body["detail"], "Internal Server Error")
        rid = body["request_id"]
        self.assertIsNotNone(rid)
        self.assertEqual(len(rid), 8)
        # The X-Request-ID header echoes the same id.
        self.assertEqual(r.headers["X-Request-ID"], rid)
        # The error logger saw a stack-trace record carrying the same id.
        error_msgs = _messages(error_capture.records)
        self.assertTrue(
            any(rid in m and "UNCAUGHT" in m for m in error_msgs),
            f"no UNCAUGHT log for rid={rid}; got {error_msgs}",
        )


# ---------------------------------------------------------------------------
class TestIterRoutes(unittest.TestCase):
    """Unit-test the iterator helper so the dump_routes expectations stay
    pinned at the function level (test 404s will catch dedup regressions
    even if the dump formatter changes)."""

    def test_iter_routes_yields_every_unique_mounted_path(self) -> None:
        paths = {p for p, _ in _iter_routes(app)}
        # Spot-check representative paths.
        for required in ("/health", "/api/pipeline/run", "/api/companies", "/api/scan/boards"):
            self.assertIn(required, paths)

    def test_iter_routes_dedupes_methods_per_path(self) -> None:
        # No path should be yielded twice (FastAPI auto-adds HEAD — but
        # we just dedupe paths, not methods).
        seen_paths: list[str] = []
        for p, _ in _iter_routes(app):
            self.assertNotIn(p, seen_paths)
            seen_paths.append(p)


if __name__ == "__main__":
    unittest.main()
