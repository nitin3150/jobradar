from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

# ProductHunt's daily leaderboard HTML is the most stable surface for
# "new products / new funding" signals without needing an API key. We scrape
# outbound links to product pages, which is the part least likely to mutate.


SOURCE = "producthunt"
URL = "https://www.producthunt.com/leaderboard/daily"


def _build_opportunity(title: str, organization: str, url: str, *, published: str | None = None) -> dict[str, Any]:
    return {
        "id": f"{SOURCE}:{url}" if url else f"{SOURCE}:{title}",
        "source": SOURCE,
        "category": "funding",
        "title": title,
        "organization": organization,
        "url": url,
        "location": "Remote",
        "tags": ["funding", "producthunt"],
        "description": "",
        "published": published or datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": 0.6,
    }


def scan(limit: int = 30) -> list[dict[str, Any]]:
    try:
        response = httpx.get(URL, timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    import re
    # Outbound links point at the product's own page (not internal producthunt.com routes).
    for match in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html):
        href, text = match.groups()
        lowered_href = href.lower()
        if "producthunt.com" in lowered_href:
            continue
        if "mailto:" in lowered_href or "javascript:" in lowered_href:
            continue
        cleaned_text = text.strip()
        if len(cleaned_text) <= 3 or len(cleaned_text) > 120:
            continue
        if href in seen:
            continue
        seen.add(href)
        items.append(_build_opportunity(cleaned_text, "Product Hunt", href))

    return items[:limit]
