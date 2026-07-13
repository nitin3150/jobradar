"""One-off scraper for the user's personal **MyGreenhouse** candidate dashboard.

URL pattern
===========

::

    https://my.greenhouse.io/jobs?query=<text>&date_posted=<window>

Output is a normalized JSON array (one object per job posting) that
matches the panel-of-key-fields shape emitted by
:func:`pipeline.nodes.jobs_boards.greenhouse.fetch` so anything that
already reads from that pipeline (the React JobCard, the S3 leads
bucket, the LLM scoring service) can consume the output without an
adapter.

Auth model
==========

The dashboard is gated by ``/users/sign_in`` — an unauthenticated
probe via ``httpx.Client`` returns a ``302`` redirect to that URI,
which the client silently follows. To run this script you must
export the ``Cookie`` header from a logged-in Chrome session as the
``MY_GREENHOUSE_COOKIE`` environment variable (see the user-facing
instructions printed when the var is unset).

Greenhouse Software does **not** expose a public API for the
candidate dashboard (no OAuth, no per-user API key); the
Harvest/Job Board APIs are company-facing only. Cookie replay is
the only path; the cookie is HttpOnly and rotates on logout,
typically lasting a few days to ~1 month between logouts.

Response shapes
===============

The page's response shape changes across Greenhouse product
releases; this script tries three in order:

1. ``Content-Type: application/json`` — most direct, parse + emit.
2. Inline ``<script type="application/json">`` bootstrap blob
   (Rails/Hydra pattern; many of Greenhouse's dashboard views
   inline page state this way).
3. Plain HTML DOM via ``BeautifulSoup`` with conservative selectors.

If none of the three yield jobs, the script aborts with a
diagnostic (``--debug`` prints the response body start so the
operator can update selectors / JSON keys for their version of
the dashboard).

Usage
=====

::

    # 1. Log in to https://my.greenhouse.io/jobs in Chrome.
    # 2. DevTools → Network → reload → click the top /jobs request.
    # 3. Copy the request's Cookie header value.
    # 4. Export and run:
    export MY_GREENHOUSE_COOKIE='_greenhouse_session=abc...; _gh_session=xyz...'
    cd backend
    python -m scripts.scrape_my_greenhouse \\
        --query engineer --date-posted past_day \\
        --output /tmp/greenhouse_jobs.json

Cost model
==========

One HTTP round-trip per page (default cap 5 pages, configurable
via ``--max-pages``). The dashboard is rate-limited separately from
``boards-api.greenhouse.io`` and serves a single user per session,
so a 1-2 second ``time.sleep`` between pages is plenty.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from bs4 import BeautifulSoup

# ---------- constants ----------
DEFAULT_BASE_URL: Final = "https://my.greenhouse.io/jobs"
DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
COOKIE_ENV_VAR: Final = "MY_GREENHOUSE_COOKIE"
RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
AUTH_FAILURE_STATUS: Final = frozenset({401, 403})
MAX_RETRIES: Final = 3
MAX_DESCRIPTION_CHARS: Final = 600
DEFAULT_MAX_PAGES: Final = 5

# Bootstrap-JSON key paths to try, in order. Greenhouse has reshuffled
# these names across releases; we cover BOTH the snake_case AND the
# camelCase variant of each name so we don't miss when a JS-side
# serializer uses the latter (the dashboard's React frontend often
# emits ``savedJobs`` / ``trackedJobs`` / ``jobPostings`` while the
# older Rails-side rails serializers use ``saved_jobs`` / etc).
#
# Operator preference: the dashboard landing page is the "My Jobs"
# view (saved / tracked / applied), so those come first; the flat
# ``jobs`` key and the space-wrapped ``results.jobs`` / ``data.jobs``
# are kept as legacy fallbacks for older Greenhouse portal releases.
_BOOTSTRAP_JOB_KEY_PATHS: Final = (
    ("saved_jobs",),
    ("savedJobs",),
    ("tracked_jobs",),
    ("trackedJobs",),
    ("applied_jobs",),
    ("appliedJobs",),
    ("job_postings",),
    ("jobPostings",),
    ("jobs",),
    ("Jobs",),
    ("results", "jobs"),
    ("results", "Jobs"),
    ("data", "jobs"),
    ("data", "Jobs"),
)

logger = logging.getLogger("jobradar.scrape_my_greenhouse")


# ---------- auth ----------
def _require_cookie() -> str:
    """Read ``MY_GREENHOUSE_COOKIE`` and abort with usage instructions if empty.

    The error message is intentionally multi-line and surfaces the
    exact steps to copy the cookie out of Chrome DevTools, because
    "set the env var" is the only thing operators ever get stuck on
    for this script.
    """
    raw = os.environ.get(COOKIE_ENV_VAR, "").strip()
    if not raw:
        sys.stderr.write(
            f"\n[my-gh] {COOKIE_ENV_VAR} is not set — cannot reach the authenticated dashboard.\n"
            "\n"
            "To use this script:\n"
            "  1. Open https://my.greenhouse.io/jobs in Chrome while logged in.\n"
            "  2. Open DevTools → Network → reload the page → click the /jobs request.\n"
            "  3. Under 'Request Headers', copy the entire `Cookie:` value.\n"
            "  4. Export it before running:\n"
            "\n"
            f"       export {COOKIE_ENV_VAR}='_greenhouse_session=abc123; _gh_session=xyz789; ...'\n"
            "\n"
            "The cookie expires when you log out of my.greenhouse.io. Treat it like a\n"
            "password: do NOT commit it; your .env is git-ignored so that's the right home.\n"
            "\n"
        )
        sys.stderr.flush()
        sys.exit(2)
    return raw


# ---------- HTTP ----------
def _build_client(cookie_value: str, *, timeout: float = 30.0) -> httpx.Client:
    """Build a per-process ``httpx.Client`` with the cookie header attached.

    We deliberately do NOT reuse :func:`utils.http.build_client` —
    that one carries the ``jobradar-scanner/1.0`` User-Agent which
    Greenhouse's edge WAF rejects for the dashboard (the dashboard
    is a separate Rails app from the public boards API and has its
    own UA allowlist biased toward real-browser strings).
    """
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # ``Cookie:`` here mirrors what the browser sent on the
            # authenticated page request. ``httpx`` does not URL-
            # encode cookie values; we trust the operator's pasted
            # string verbatim because they copied it from DevTools
            # where Chrome's display is already URL-safe.
            "Cookie": cookie_value,
        },
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Read ``Retry-After`` (seconds form) or exponential backoff.

    Mirrors the math in :func:`utils.http._retry_after_seconds` so
    behavior is consistent across the two boards-related scripts.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return min(1.0 * (2 ** attempt), 30.0)


def fetch_page(client: httpx.Client, url: str) -> httpx.Response:
    """GET ``url`` with bounded retry on 429/5xx and explicit auth-failure surfacing.

    401/403 short-circuit with :class:`PermissionError` because the
    most common cause is an expired cookie — the operator wants a
    one-liner "refresh the cookie" reminder, not a retried failure.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        response = client.get(url)
        if response.status_code in AUTH_FAILURE_STATUS:
            raise PermissionError(
                f"auth wall at {url}: HTTP {response.status_code}. "
                f"Your {COOKIE_ENV_VAR} may be expired or contain only "
                "anonymous-session cookies — re-copy the Cookie header "
                "from a logged-in Chrome DevTools session and retry."
            )
        if response.status_code in RETRYABLE_STATUS:
            last_exc = httpx.HTTPStatusError(
                f"retryable status {response.status_code}",
                request=response.request,
                response=response,
            )
            if attempt < MAX_RETRIES:
                logger.info(
                    "retrying %s after %.2fs (status %d, attempt %d/%d)",
                    url,
                    _retry_after_seconds(response, attempt),
                    response.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(_retry_after_seconds(response, attempt))
                continue
            raise last_exc
        response.raise_for_status()
        return response
    raise RuntimeError(f"exhausted {MAX_RETRIES} retries on {url}: {last_exc}")


# ---------- response parsers ----------
def _query_url(base: str, *, query: str | None, date_posted: str | None, page: int) -> str:
    """Build the URL for a single page of the dashboard.

    ``page=1`` is omitted — Greenhouse's first-page URL has no
    ``page`` parameter, and sticking to that convention avoids a
    redirect on page 1.
    """
    params: dict[str, str] = {}
    if query:
        params["query"] = query
    if date_posted:
        params["date_posted"] = date_posted
    if page > 1:
        params["page"] = str(page)
    if not params:
        return base
    return f"{base}?{urlencode(params)}"


def _coerce_str(value: Any) -> str:
    """Best-effort string coercion for JSON bootstrap fields that may
    arrive as ``None``, missing, ``int``, or a nested object.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def _extract_bootstrap_json(html: str) -> dict | None:
    """Pull a Rails/Hydra-style ``<script type="application/json">`` blob.

    Many Greenhouse dashboard views inline the page state this way
    to feed the React/Vue frontend, e.g.::

        <script id="bootstrap" type="application/json">
            {"jobs": [...], "filters": {...}}
        </script>

    We grab the FIRST ``application/json`` script tag whose body
    parses as a JSON object — multiple tags on a page are common
    (one per island), but only the page-state one will be a dict at
    the top level. ``application/json`` is used over ``text/json`` to
    match what Greenhouse actually emits.
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        if script.get("type") != "application/json":
            continue
        text = (script.string or script.get_text() or "").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
        # Wrap top-level lists — some Greenhouse views inline the
        # jobs array directly without a wrapping object.
        if isinstance(parsed, list):
            return {"jobs": parsed}
    return None


def _find_jobs_node(payload: dict) -> list | None:
    """Walk :data:`_BOOTSTRAP_JOB_KEY_PATHS` against a bootstrap payload.

    Returns the first list-typed node reachable through a key path
    in the constant. Returns ``None`` so the caller can decide
    between "no list at all" (abort) and "empty list" (end of pages).
    """
    for path in _BOOTSTRAP_JOB_KEY_PATHS:
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, list):
            return node
    return None


def _coerce_published_at(raw: dict) -> str | None:
    """Reduce Greenhouse's various date-bearing fields to a string OR ``None``.

    Greenhouse emits ``published_at`` as either an ISO timestamp
    string (``"2026-07-11T12:34:56Z"``) OR a calendar-shaped dict
    (``{"name": "July 11, 2026", "iso": "2006-07-11"}``) across
    releases. ``date_posted`` only ever arrives as a string;
    ``updated_at`` rarely carries a calendar-shape dict. The
    runner downstream consumes all four via
    :func:`utils.time_check.parse_published_at`, an ISO parser,
    so when Greenhouse hands us a dict we MUST prefer the
    machine-readable ``iso`` key over the human-readable ``name``
    key — picking ``name`` would yield display strings like
    ``"July 11, 2026"`` that the ISO parser silently returns
    ``None`` on, regressing the same "old jobs with new dates"
    symptom the existing fetcher's two-timestamp split was
    designed to prevent.
    """
    value = (
        raw.get("published_at")
        or raw.get("date_posted")
        or raw.get("created_at")
        or raw.get("updated_at")
    )
    if value is None:
        return None
    if isinstance(value, dict):
        # Precedence is machine-readable first — ``iso`` wins, then
        # the older ``value`` key Greenhouse used before the iso
        # rename, then ``name`` last as a display-only fallback.
        value = (
            value.get("iso")
            or value.get("value")
            or value.get("name")
            or ""
        )
    value = str(value).strip()
    return value or None


def _coerce_location(raw: dict) -> str | None:
    """Reduce Greenhouse's ``location`` field to a string OR ``None``.

    Older portal views return a simple string like ``"Remote (US)"``;
    newer portal views return a dict ``{"name": "Remote (US)", ...}``.
    Downstream consumers (the React ``JobCard``, ``FilterBar``)
    expect a string — a dict here would render as ``[object Object]``
    in the React subtitle. Reduce to a flat string in all cases.
    """
    value = raw.get("location") or raw.get("location_name")
    if not value:
        return None
    if isinstance(value, dict):
        value = (
            value.get("name")
            or value.get("display_name")
            or value.get("city")
            or ""
        )
    value = str(value).strip()
    return value or None


def _coerce_departments(raw: dict) -> list[str] | None:
    """Reduce Greenhouse's ``departments`` field to a list of strings OR ``None``.

    Greenhouse emits ``departments`` as a list of dicts
    (``[{"name": "Engineering"}]``) OR a plain string (``"Engineering"``)
    OR absent (``None``) — all three are observed across releases.
    The runner's :func:`utils.filters.filter_roles` matches on
    string keywords via :data:`utils.filters.DEFAULT_RELEVANT_PATTERNS`,
    so any non-string here silently breaks role matching. Reduce
    to a flat list of strings.
    """
    value = raw.get("departments")
    if not value:
        return None
    if isinstance(value, list):
        names = [
            (item.get("name") if isinstance(item, dict) else str(item))
            for item in value
        ]
        cleaned = [str(n).strip() for n in names if n and str(n).strip()]
        return cleaned or None
    if isinstance(value, dict):
        single = value.get("name")
        return [str(single).strip()] if single else None
    single = str(value).strip()
    return [single] if single else None


def _normalize_bootstrap_job(raw: Any) -> dict | None:
    """Map a single bootstrap-JSON job dict to the project panel-of-key-fields shape.

    Field-name guesses are generous — Greenhouse has used
    ``"absolute_url"``, ``"job_url"``, ``"url"``, ``"data_url"`` and
    ``"id"`` / ``"requisition_id"`` / ``"job_id"`` across releases.
    We pick whichever is set. Returns ``None`` for non-dict inputs
    so the caller can ``continue`` cleanly.

    ``published_at`` / ``location`` / ``departments`` are explicitly
    coerced to plain strings (or list of strings) so the output
    matches the panel-of-key-fields shape produced by
    :func:`pipeline.nodes.jobs_boards.greenhouse.fetch`. Dropping
    the raw Greenhouse dicts (e.g. ``{"name": "San Francisco"}``
    for ``location``) would silently break downstream string-keyword
    role matching in :func:`utils.filters.filter_roles` and would
    render as ``[object Object]`` in the React JobCard subtitles.
    """
    if not isinstance(raw, dict):
        return None
    url = (
        _coerce_str(raw.get("absolute_url"))
        or _coerce_str(raw.get("job_url"))
        or _coerce_str(raw.get("url"))
        or _coerce_str(raw.get("data_url"))
    )
    if not url and not raw.get("title"):
        return None
    description_raw = (
        raw.get("content") or raw.get("description") or raw.get("body") or ""
    )
    if isinstance(description_raw, str):
        description = description_raw[:MAX_DESCRIPTION_CHARS]
    else:
        description = _coerce_str(description_raw)[:MAX_DESCRIPTION_CHARS]
    return {
        "id": _coerce_str(
            raw.get("id") or raw.get("job_id") or raw.get("requisition_id")
        ) or (url.rstrip("/").split("/")[-1] if url else ""),
        "title": _coerce_str(raw.get("title") or raw.get("name")),
        "company": _coerce_str(
            raw.get("company_name") or raw.get("department_name") or raw.get("company")
        ),
        "url": url,
        "published_at": _coerce_published_at(raw),
        "location": _coerce_location(raw),
        "departments": _coerce_departments(raw),
        "description": description,
    }


def _parse_jobs_from_bootstrap(payload: dict) -> list[dict]:
    """Normalize a bootstrap payload's jobs array to the project shape."""
    node = _find_jobs_node(payload)
    if node is None:
        return []
    out: list[dict] = []
    for raw in node:
        normalized = _normalize_bootstrap_job(raw)
        if normalized is not None:
            out.append(normalized)
    return out


def _parse_jobs_from_html(html: str) -> list[dict]:
    """Best-effort BeautifulSoup DOM scrape when no JSON is available.

    Selectors are conservative Greenhouse-class guesses; the
    ``--debug`` printout shows what cards were matched. If the
    dashboard restructured, the operator tweaks here — not in any
    upstream pipeline.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(
        ".job, .job-posting, [data-job-id], .job-row, .job-listing, "
        "div[class*='job-card']"
    )
    jobs: list[dict] = []
    for card in cards:
        anchor = card.select_one("a[href*='/jobs/']")
        href = anchor.get("href", "") if anchor else ""
        if href and not href.startswith("http"):
            href = f"https://boards.greenhouse.io{href}"
        title_el = card.select_one(".job-title, h2, h3, .title, [class*='title']")
        loc_el = card.select_one(
            ".job-location, .location, [data-location], [class*='location']"
        )
        dept_el = card.select_one(
            ".job-department, .department, [class*='department']"
        )
        company_el = card.select_one(
            ".job-company, .company, [class*='company']"
        )
        jobs.append(
            {
                "id": _coerce_str(card.get("data-job-id"))
                or (href.rstrip("/").split("/")[-1] if href else ""),
                "title": title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "url": href,
                "published_at": card.get("data-published-at") or card.get("data-date"),
                "location": loc_el.get_text(strip=True) if loc_el else None,
                "departments": [dept_el.get_text(strip=True)] if dept_el else None,
                "description": "",
            }
        )
    return jobs


# ---------- public entry point ----------
def scrape(
    client: httpx.Client,
    *,
    query: str | None = None,
    date_posted: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    max_pages: int = DEFAULT_MAX_PAGES,
    debug: bool = False,
) -> list[dict]:
    """Fetch all matching jobs across up to ``max_pages`` pages.

    Pagination: stops when a page returns zero jobs OR when
    ``max_pages`` is reached. ``max_pages=1`` means single-page.
    The 1-second inter-page sleep is intentional politeness toward
    ``my.greenhouse.io`` (a separate user-facing Rails app, not
    shared with the boards API).
    """
    all_jobs: list[dict] = []
    for page in range(1, max_pages + 1):
        url = _query_url(base_url, query=query, date_posted=date_posted, page=page)
        try:
            resp = fetch_page(client, url)
        except PermissionError as exc:
            raise  # surface verbatim — the operator needs the cookie-refresh hint
        body = resp.text
        ctype = resp.headers.get("content-type", "").lower()

        # Path 1: response is JSON. Some Greenhouse endpoints expose
        # a JSON view behind the same URL with an Accept header
        # negotiation; if that's what came back, parse straight.
        if "application/json" in ctype:
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                logger.info(
                    "page %d: Content-Type claims JSON but decode failed (%s); "
                    "falling back to inline-bootstrap scan",
                    page,
                    exc,
                )
            else:
                payload = data if isinstance(data, dict) else {"jobs": data}
                jobs = _parse_jobs_from_bootstrap(payload)
                logger.info(
                    "page %d: parsed %d jobs from JSON Content-Type response",
                    page,
                    len(jobs),
                )
                all_jobs.extend(jobs)
                if not jobs:
                    break
                if page < max_pages:
                    time.sleep(1.0)
                continue

        # Path 2: HTML with inline bootstrap JSON.
        bootstrap = _extract_bootstrap_json(body)
        if bootstrap is not None:
            jobs = _parse_jobs_from_bootstrap(bootstrap)
            logger.info(
                "page %d: extracted %d jobs from inline bootstrap JSON",
                page,
                len(jobs),
            )
            all_jobs.extend(jobs)
            if not jobs:
                break
            if page < max_pages:
                time.sleep(1.0)
            continue

        # Path 3: HTML DOM scrape.
        jobs = _parse_jobs_from_html(body)
        logger.info(
            "page %d: parsed %d jobs via HTML DOM selectors", page, len(jobs)
        )
        all_jobs.extend(jobs)
        if not jobs:
            break
        if page < max_pages:
            time.sleep(1.0)

    if debug:
        sys.stderr.write(
            f"[my-gh] debug body start (first page only, first 400 chars):\n"
            f"{body[:400]!r}\n"
        )
        sys.stderr.flush()

    return all_jobs


# ---------- CLI ----------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.scrape_my_greenhouse",
        description=(
            "Scrape the user's authenticated MyGreenhouse dashboard and emit "
            "the matching jobs as a normalized JSON array."
        ),
    )
    parser.add_argument(
        "--query",
        default="engineer",
        help=(
            "Free-text query matching Greenhouse's on-page filter (default 'engineer'). "
            "Empty string disables the filter; pass '' literally."
        ),
    )
    parser.add_argument(
        "--date-posted",
        default="past_day",
        help=(
            "Greenhouse's date_posted window. Common values: past_day, past_week, "
            "past_month, past_3_months. Empty string disables. (default 'past_day')"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Maximum pages to walk. Default {DEFAULT_MAX_PAGES}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON to this file instead of stdout.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print first 400 chars of the page body to stderr for selector tuning.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Override the dashboard URL (mostly for tests).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="[my-gh] %(asctime)s %(levelname)s %(message)s",
    )

    cookie = _require_cookie()
    client = _build_client(cookie)
    try:
        try:
            jobs = scrape(
                client,
                query=args.query or None,
                date_posted=args.date_posted or None,
                base_url=args.base_url,
                max_pages=max(1, args.max_pages),
                debug=args.debug,
            )
        except PermissionError as exc:
            sys.stderr.write(f"[my-gh] {exc}\n")
            sys.stderr.flush()
            # Exit code 2 — same as ``_require_cookie``'s missing-env
            # case. Both are operator-fixable user-input conditions
            # ("refresh your MY_GREENHOUSE_COOKIE"); unifying on 2 so
            # shell wrappers can pattern-match on a single code.
            return 2
    finally:
        client.close()

    payload = json.dumps(jobs, indent=2, ensure_ascii=False, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        logger.info("wrote %d jobs to %s", len(jobs), args.output)
    else:
        print(payload)

    if not jobs:
        sys.stderr.write(
            "\n[my-gh] no jobs extracted — possibilities:\n"
            "  - Cookie is valid but no role matches the query/date filter\n"
            "  - The dashboard HTML/DOM has restructured (re-run with --debug\n"
            "    to see what selectors fired, then tweak _parse_jobs_from_html)\n"
            "  - The bootstrap JSON key path shifted (re-run with --debug, then\n"
            "    add the new key to _BOOTSTRAP_JOB_KEY_PATHS)\n"
        )
        sys.stderr.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
