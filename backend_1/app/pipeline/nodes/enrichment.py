"""Node 2: Enrichment.

For each raw company, enriches with:
- Website content extraction (description)
- Twitter/X hiring signal search via Apify
- Founder information extraction
"""

import logging

import httpx

from app.config import settings as app_settings
from app.pipeline.state import PipelineState
from app.utils.backoff import with_backoff

logger = logging.getLogger(__name__)


async def enrichment_node(
    state: PipelineState,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Enrich raw companies with website data and hiring signals."""
    raw_companies = state.get("raw_companies", [])
    errors = list(state.get("errors", []))
    enriched = []

    for company in raw_companies:
        try:
            enriched_company = await _enrich_company(company, http_client)
            enriched.append(enriched_company)
        except Exception as e:
            logger.warning(f"Failed to enrich {company.get('name', '?')}: {e}")
            errors.append(f"enrichment: {company.get('name', '?')}: {e}")
            # Keep the raw company data even if enrichment fails
            enriched.append(company)

    stats = {**state.get("stats", {}), "enriched": len(enriched)}
    return {"enriched_companies": enriched, "errors": errors, "stats": stats}


@with_backoff(max_retries=1, base_delay=1.0)
async def _fetch_website_description(
    url: str, http_client: httpx.AsyncClient
) -> str | None:
    """Fetch a company website and extract a description from meta tags or text."""
    try:
        resp = await http_client.get(url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text[:10000]  # Limit to first 10KB

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "lxml")

        # Try meta description
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            return meta["content"].strip()[:500]

        # Try og:description
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            return og["content"].strip()[:500]

        # Fallback: first paragraph
        p = soup.find("p")
        if p:
            return p.get_text(strip=True)[:500]

    except Exception:
        pass
    return None


async def _search_twitter_signals(
    company_name: str, http_client: httpx.AsyncClient
) -> list[str]:
    """Search for hiring signals on Twitter via Apify (if configured)."""
    if not app_settings.apify_api_key:
        return []

    try:
        headers = {"Authorization": f"Bearer {app_settings.apify_api_key}"}
        run_input = {
            "searchTerms": [f'"{company_name}" hiring OR "open roles" OR "join us"'],
            "maxTweets": 10,
        }
        resp = await http_client.post(
            "https://api.apify.com/v2/acts/apify~twitter-scraper/run-sync-get-dataset-items",
            json=run_input,
            headers=headers,
            timeout=60.0,
        )
        if resp.status_code == 200:
            tweets = resp.json()
            return [t.get("text", "")[:300] for t in tweets[:5] if t.get("text")]
    except Exception as e:
        logger.debug(f"Twitter search failed for {company_name}: {e}")

    return []


async def _enrich_company(
    company: dict, http_client: httpx.AsyncClient | None
) -> dict:
    """Enrich a single company with additional data."""
    enriched = {**company}

    if http_client:
        # Fetch website description if we have a URL but no description
        website = company.get("website", "")
        if website and not company.get("description"):
            if website.startswith("http"):
                desc = await _fetch_website_description(website, http_client)
                if desc:
                    enriched["description"] = desc

        # Search for Twitter hiring signals
        name = company.get("name", "")
        if name:
            signals = await _search_twitter_signals(name, http_client)
            existing_signals = company.get("hiring_signals", [])
            if isinstance(existing_signals, list):
                enriched["hiring_signals"] = existing_signals + signals
            else:
                enriched["hiring_signals"] = signals

    return enriched
