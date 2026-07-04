"""Swappable web-search adapter for ATS slug discovery.

Backends (config.discovery_search_backend):
  - "playwright": headless Google (falls back to Bing on block). No API key,
    but Google WILL serve CAPTCHA in headless/cron — failures are logged loudly,
    never silent. Swap to "serper" for reliable scheduled runs.
  - "serper":     serper.dev JSON Google API (needs SERPER_API_KEY).

All backends return a flat list of result URLs. Slug extraction happens in
discovery.py, so a backend only needs to surface candidate links.
"""
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Signs Google/Bing served a block/consent wall instead of results.
_BLOCK_MARKERS = ("unusual traffic", "recaptcha", "/sorry/", "detected unusual", "captcha")


def qdr_param(freshness_hours: int) -> str:
    """Google `tbs=qdr:` recency value for a window in hours.

    NOTE: qdr filters by when Google *reindexed* a page, not when a job was
    posted. Index lag is hours-to-days, so a tight window discovers little.
    Real freshness comes from local URL dedup in the fetch loop, not this.
    """
    if freshness_hours <= 0:
        return ""
    if freshness_hours < 48:
        return f"h{freshness_hours}"
    return f"d{max(1, freshness_hours // 24)}"


def _extract_hrefs(html: str) -> list[str]:
    """Pull outbound http(s) links from a SERP HTML blob."""
    hrefs = re.findall(r'href="(https?://[^"]+)"', html)
    out = []
    for h in hrefs:
        if any(bad in h for bad in ("google.com", "gstatic.com", "bing.com", "microsoft.com")):
            continue
        out.append(h)
    return out


async def _search_playwright(query: str, freshness_hours: int, browser, max_results: int) -> list[str]:
    if browser is None:
        logger.warning("Playwright search backend selected but no browser available")
        return []

    qdr = qdr_param(freshness_hours)
    tbs = f"&tbs=qdr:{qdr}" if qdr else ""
    from urllib.parse import quote_plus

    google_url = f"https://www.google.com/search?q={quote_plus(query)}{tbs}&num={max_results}&hl=en"
    context = await browser.new_context(user_agent=_UA, locale="en-US")
    try:
        page = await context.new_page()
        await page.goto(google_url, wait_until="domcontentloaded", timeout=30000)
        html = await page.content()
        landing = page.url.lower()
        if any(m in html.lower() for m in _BLOCK_MARKERS) or "/sorry/" in landing:
            logger.warning(
                f"Google blocked headless search (CAPTCHA/consent) for query: {query!r}. "
                f"Falling back to Bing. For reliable scheduled discovery set "
                f"discovery_search_backend=serper."
            )
            return await _search_bing_via_page(page, query, max_results)
        links = _extract_hrefs(html)
        logger.info(f"Playwright/Google returned {len(links)} links for {query!r}")
        return links[:max_results]
    finally:
        await context.close()


async def _search_bing_via_page(page, query: str, max_results: int) -> list[str]:
    """Bing is more tolerant of headless traffic; used as a fallback."""
    from urllib.parse import quote_plus

    try:
        await page.goto(
            f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        html = await page.content()
        if any(m in html.lower() for m in _BLOCK_MARKERS):
            logger.warning(f"Bing also blocked headless search for query: {query!r}")
            return []
        links = _extract_hrefs(html)
        logger.info(f"Bing fallback returned {len(links)} links for {query!r}")
        return links[:max_results]
    except Exception as e:
        logger.error(f"Bing fallback failed for {query!r}: {e}")
        return []


async def _search_serper(
    query: str, freshness_hours: int, http_client: httpx.AsyncClient, api_key: str, max_results: int
) -> list[str]:
    if not api_key:
        logger.warning("serper backend selected but SERPER_API_KEY is empty")
        return []
    qdr = qdr_param(freshness_hours)
    payload = {"q": query, "num": max_results}
    if qdr:
        payload["tbs"] = f"qdr:{qdr}"
    try:
        resp = await http_client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        links = [item.get("link", "") for item in data.get("organic", []) if item.get("link")]
        logger.info(f"Serper returned {len(links)} links for {query!r}")
        return links[:max_results]
    except Exception as e:
        logger.error(f"Serper search failed for {query!r}: {e}")
        return []


async def search_urls(
    query: str,
    *,
    freshness_hours: int,
    backend: str,
    browser=None,
    http_client: httpx.AsyncClient | None = None,
    serper_key: str = "",
    max_results: int = 30,
) -> list[str]:
    """Run a web search and return candidate result URLs.

    `backend="auto"` resolves to `serper` when SERPER_API_KEY is configured and
    to `playwright` otherwise, so the user can opt into reliable scheduled
    discovery by setting one env var without touching settings.py.
    """
    # `.strip()` keeps a whitespace-only `SERPER_API_KEY="   "` from misleading
    # the resolver into the serper path and burning a round-trip on a 401.
    if backend == "auto":
        backend = "serper" if ((serper_key or "").strip()) else "playwright"
        logger.info(f"discovery_search_backend=auto resolved to {backend!r}")

    if backend == "serper":
        return await _search_serper(query, freshness_hours, http_client, serper_key, max_results)
    if backend == "playwright":
        return await _search_playwright(query, freshness_hours, browser, max_results)
    logger.error(f"Unknown discovery_search_backend: {backend!r}")
    return []



