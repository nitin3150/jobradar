"""Shared HTTP helpers for the job-board scanners.

Scanners hit a handful of hosts (greenhouse / lever / ashby) across thousands
of orgs. Two production hazards this module addresses:

* a new TCP+TLS handshake per request -> reuse one pooled ``httpx.Client``;
* rate limiting (429) and transient upstream blips (5xx) -> bounded retry with
  backoff that respects ``Retry-After``.

``get_json`` is the single entry point. It raises ``httpx.HTTPStatusError`` for
non-retryable HTTP errors (so callers can classify 404/410 as "missing"), and
``ValueError`` when a 200 response is not the JSON the board API should return
(e.g. a Cloudflare / maintenance HTML page).
"""

import os
import time

import httpx

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0

# Default per-request timeout. ``BOARDS_HTTP_TIMEOUT`` overrides from
# the boards runner / boards_scan.py so the daily slow-org tier can
# spend up to 30s on a sluggish org without burning the hourly 10s
# budget on the same org. Read at module-import time so the boards
# runner picks it up before ``build_client()`` is called.
DEFAULT_HTTP_TIMEOUT = 10.0


def build_client(timeout: float | None = None) -> httpx.Client:
    """A pooled, thread-safe client shared across scanner threads.

    ``timeout`` defaults to ``BOARDS_HTTP_TIMEOUT`` if set, else
    :data:`DEFAULT_HTTP_TIMEOUT` (10s). The boards runner calls
    ``build_client()`` with no arg so the env var drives the runtime
    value; tests can still pass an explicit float.
    """
    if timeout is None:
        raw = os.environ.get("BOARDS_HTTP_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                timeout = DEFAULT_HTTP_TIMEOUT
        else:
            timeout = DEFAULT_HTTP_TIMEOUT
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "jobradar-scanner/1.0"},
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), BACKOFF_CAP)
        except ValueError:
            pass  # HTTP-date form is uncommon here; fall back to backoff
    return min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)


def get_json(client: httpx.Client, url: str, max_retries: int = MAX_RETRIES, sleep=time.sleep):
    """GET ``url`` and return parsed JSON, retrying on 429/5xx.

    Raises ``httpx.HTTPStatusError`` on a final non-retryable status, and
    ``ValueError`` if a 2xx body is not valid JSON.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        response = client.get(url)
        if response.status_code in RETRYABLE_STATUS:
            last_exc = httpx.HTTPStatusError(
                f"retryable status {response.status_code}", request=response.request, response=response
            )
            if attempt < max_retries:
                sleep(_retry_after_seconds(response, attempt))
                continue
            raise last_exc
        response.raise_for_status()
        try:
            return response.json()
        except Exception as exc:
            raise ValueError(f"non-JSON response from {url}: {exc}") from exc
    raise last_exc  # unreachable, but keeps type-checkers happy
