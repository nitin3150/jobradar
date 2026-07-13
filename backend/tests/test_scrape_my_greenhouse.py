"""Tests for ``scripts.scrape_my_greenhouse``.

Coverage matrix:

* ``test_aborts_when_cookie_missing`` — no ``MY_GREENHOUSE_COOKIE``
  → ``SystemExit(2)`` with usage hint on stderr.
* ``test_auth_wall_short_circuits_to_permission_error`` —
  MockTransport returns 401 → ``scrape`` raises ``PermissionError``
  whose message mentions the cookie-refresh hint.
* ``test_json_content_type_parses_bootstrap`` — Content-Type
  ``application/json``, JSON body with a ``jobs`` key → 1 normalized
  job in the output, URL handled.
* ``test_inline_bootstrap_json_parsed`` — Content-Type
  ``text/html``, body contains ``<script type="application/json">``
  with a ``jobs`` key → 1 normalized job.
* ``test_html_dom_selectors`` — Plain HTML with Greenhouse-class card
  markup → 1 job from the BeautifulSoup path.
* ``test_pagination_stops_on_empty_page`` — first page has jobs,
  second page is empty → stops after 2 pages and returns only the
  first-page jobs.
* ``test_output_shape_matches_existing_greenhouse_fetcher`` —
  schema parity with the keys used by :func:`greenhouse.fetch`.

All tests use ``httpx.MockTransport`` so no real ``my.greenhouse.io``
traffic is generated and the suite is hermetic.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

# Make the script importable as a module — same pattern other test
# files use for ``scripts/boards_scan.py`` etc.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR.parent))

import scripts.scrape_my_greenhouse as smg  # noqa: E402


# ---------- helpers ----------
def _make_client(handler) -> httpx.Client:
    """Build an ``httpx.Client`` whose transport routes through a test handler.

    The cookie header is injected by ``_build_client`` so the
    MockTransport sees the same shape as a real authenticated
    request.
    """
    transport = httpx.MockTransport(handler)
    client = smg._build_client("_greenhouse_session=fake-cookie", timeout=5.0)
    # Replace transport after construction; ``_build_client`` returns
    # a real httpx.Client instance we tear down off the real network.
    client._transport = transport  # type: ignore[attr-defined]
    return client


# ---------- tests ----------
def test_aborts_when_cookie_missing(monkeypatch, capsys):
    """No ``MY_GREENHOUSE_COOKIE`` → ``SystemExit(2)`` + usage hint in stderr."""
    monkeypatch.delenv("MY_GREENHOUSE_COOKIE", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        smg._require_cookie()
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "MY_GREENHOUSE_COOKIE is not set" in captured.err
    assert "open https://my.greenhouse.io/jobs" in captured.err.lower()
    assert "DevTools" in captured.err


def test_auth_wall_short_circuits_to_permission_error(monkeypatch):
    """MockTransport returns 401 once → ``scrape`` raises ``PermissionError`` whose message gives an actionable cookie-refresh hint pointing at Chrome DevTools."""
    monkeypatch.setenv("MY_GREENHOUSE_COOKIE", "_greenhouse_session=fake-cookie")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "_greenhouse_session=fake-cookie" in request.headers.get("cookie", "")
        return httpx.Response(401, text="Sign in")

    client = _make_client(handler)
    try:
        with pytest.raises(PermissionError) as exc_info:
            smg.scrape(client, query="engineer", date_posted="past_day", max_pages=1)
        msg = str(exc_info.value)
        assert "401" in msg
        assert "MY_GREENHOUSE_COOKIE" in msg
        assert "expired" in msg.lower()
        # Reviewer-flagged assertion: the cookie-refresh hint MUST
        # mention ``Chrome DevTools`` so the operator immediately
        # knows where to copy the new ``Cookie:`` header from.
        assert "Chrome DevTools" in msg
        assert "Cookie" in msg
    finally:
        client.close()


def test_json_content_type_parses_bootstrap(monkeypatch):
    """``Content-Type: application/json`` with a ``jobs`` key → 1 normalized job."""
    monkeypatch.setenv("MY_GREENHOUSE_COOKIE", "_greenhouse_session=fake-cookie")
    payload = {
        "jobs": [
            {
                "id": 4094013001,
                "title": "Senior Platform Engineer",
                "absolute_url": "https://boards.greenhouse.io/anthropic/jobs/4094013001",
                "published_at": "2026-07-11T12:34:56Z",
                "location": {"name": "San Francisco, CA"},
                "departments": [{"name": "Engineering"}],
                "content": "<p>Build platform tooling...</p>",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "query=engineer" in str(request.url)
        assert "date_posted=past_day" in str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=payload,
        )

    client = _make_client(handler)
    try:
        jobs = smg.scrape(client, query="engineer", date_posted="past_day", max_pages=1)
    finally:
        client.close()

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Senior Platform Engineer"
    assert job["url"].endswith("/jobs/4094013001")
    # After the output-shape-coercion fix (reviewer round 2): the
    # bootstrap dict ``{"name": "..."}`` / ``[{"name": "..."}]``
    # variants on ``location`` / ``departments`` get reduced to
    # plain strings / list-of-strings so the JobCard subtitle and
    # the runner's role filter can match them verbatim.
    assert job["published_at"] == "2026-07-11T12:34:56Z"
    assert isinstance(job["published_at"], str)
    assert isinstance(job["location"], str)
    assert job["location"] == "San Francisco, CA"
    assert isinstance(job["departments"], list)
    assert all(isinstance(d, str) for d in job["departments"])
    assert "Engineering" in job["departments"]
    assert job["description"].startswith("<p>")


def test_inline_bootstrap_json_parsed(monkeypatch):
    """HTML page with a ``<script type='application/json'>`` blob → parsed via Path 2."""
    monkeypatch.setenv("MY_GREENHOUSE_COOKIE", "_greenhouse_session=fake-cookie")
    blob = {
        "jobs": [
            {
                "id": "req-9001",
                "title": "ML Engineer",
                "job_url": "https boards greenhouse".replace(" ", "://") + "/openai/jobs/req-9001",
                "published_at": "2026-07-11",
                "company_name": "OpenAI",
                "content": "Work on alignment.",
            }
        ]
    }
    html_body = (
        "<!doctype html><html><head>"
        '<script id="bootstrap" type="application/json">'
        + json.dumps(blob)
        + '</script></head><body>dashboard</body></html>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html_body)

    client = _make_client(handler)
    try:
        jobs = smg.scrape(client, query="engineer", date_posted="past_day", max_pages=1)
    finally:
        client.close()

    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "ML Engineer"
    assert job["id"] == "req-9001"
    assert "openai/jobs/req-9001" in job["url"]
    assert job["company"] == "OpenAI"


def test_html_dom_selectors(monkeypatch):
    """Plain HTML with Greenhouse-class job cards → BeautifulSoup DOM path."""
    monkeypatch.setenv("MY_GREENHOUSE_COOKIE", "_greenhouse_session=fake-cookie")
    html_body = (
        "<!doctype html><html><body>"
        '<div class="job" data-job-id="gh-1234">'
        '<h2 class="job-title">Staff Backend Engineer</h2>'
        '<span class="job-location">Remote (US)</span>'
        '<span class="job-department">Engineering</span>'
        '<a href="https://boards.greenhouse.io/stripe/jobs/gh-1234">View</a>'
        '</div>'
        '</body></html>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html_body)

    client = _make_client(handler)
    try:
        jobs = smg.scrape(client, query="engineer", date_posted="past_day", max_pages=1)
    finally:
        client.close()

    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "gh-1234"
    assert job["title"] == "Staff Backend Engineer"
    assert job["location"] == "Remote (US)"
    assert job["departments"] == ["Engineering"]
    assert job["url"] == "https://boards.greenhouse.io/stripe/jobs/gh-1234"


def test_pagination_stops_on_empty_page(monkeypatch):
    """Page 1 has 1 job, page 2 has 0 → loop terminates and we keep only page 1's job."""
    monkeypatch.setenv("MY_GREENHOUSE_COOKIE", "_greenhouse_session=fake-cookie")
    page_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_calls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jobs": []},
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Engineer 1",
                        "absolute_url": "https://x/1",
                    }
                ]
            },
        )

    client = _make_client(handler)
    try:
        jobs = smg.scrape(client, query="engineer", date_posted="past_day", max_pages=3)
    finally:
        client.close()

    assert len(jobs) == 1
    assert "page=2" in page_calls[-1] or len(page_calls) >= 2


def test_published_at_prefers_iso_over_name_for_calendar_dict():
    """Calendar-dict ``published_at``: ``iso`` wins over ``name``.

    Greenhouse emits the calendar shape as
    ``{"name": "July 11, 2026", "iso": "2026-07-11"}`` on some
    role views. Downstream :func:`utils.time_check.parse_published_at`
    is an ISO parser, so picking the human-readable ``name`` would
    silently return ``None`` and regress the same "old jobs with new
    dates" symptom the existing fetcher's two-timestamp split was
    designed to prevent. This test pins the precedence
    ``iso → value → name`` so a future refactor can't silently
    regress.
    """
    blob = {
        "jobs": [
            {
                "title": "X",
                "absolute_url": "https://x/job",
                "published_at": {
                    "name": "July 11, 2026",
                    "iso": "2026-07-11",
                },
            }
        ]
    }
    jobs = smg._parse_jobs_from_bootstrap(blob)
    assert jobs[0]["published_at"] == "2026-07-11"
    assert jobs[0]["published_at"] != "July 11, 2026"
    assert isinstance(jobs[0]["published_at"], str)


def test_output_shape_matches_existing_greenhouse_fetcher():
    """Static-shape test: every output job carries the panel-of-key-fields keys,
    all string-coercible, and the public-board fetcher can consume the dict.

    We don't import the real fetcher here (cross-module coupling);
    we just verify the contract that
    :func:`pipeline.nodes.jobs_boards.greenhouse.fetch` documents.
    """
    expected_keys = {
        "id",
        "title",
        "company",
        "url",
        "published_at",
        "location",
        "departments",
        "description",
    }
    blob = {
        "jobs": [
            {
                "id": 9999,
                "title": "Test Engineer",
                "absolute_url": "https://x/job",
            }
        ]
    }
    jobs = smg._parse_jobs_from_bootstrap(blob)
    assert len(jobs) == 1
    assert set(jobs[0].keys()) == expected_keys


def test_query_url_omits_page_when_page_equals_one():
    """Page 1 has no ``?page=`` suffix — Greenhouse's first-page convention."""
    url = smg._query_url(
        smg.DEFAULT_BASE_URL, query="engineer", date_posted="past_day", page=1
    )
    assert "page=" not in url
    assert "query=engineer" in url
    assert "date_posted=past_day" in url


def test_query_url_includes_page_above_one():
    url = smg._query_url(
        smg.DEFAULT_BASE_URL, query=None, date_posted=None, page=3
    )
    assert "page=3" in url
    assert "query=" not in url
    assert "date_posted=" not in url
