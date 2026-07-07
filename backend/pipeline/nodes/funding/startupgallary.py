from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

SOURCE = "startupsgallery"
URL = "https://startups.gallery/news"


def _build_opportunity(title: str, organization: str, url: str) -> dict[str, Any]:
    return {
        "id": f"{SOURCE}:{url}" if url else f"{SOURCE}:{title}",
        "source": SOURCE,
        "category": "funding",
        "title": title,
        "organization": organization,
        "url": url,
        "location": "Remote",
        "tags": ["funding", "news"],
        "description": "",
        "published": datetime.now(timezone.utc).date().isoformat(),
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
    for match in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html):
        href, text = match.groups()
        cleaned_text = text.strip()
        if "startups.gallery" in href.lower():
            continue
        if len(cleaned_text) <= 3 or len(cleaned_text) > 120:
            continue
        if href in seen:
            continue
        seen.add(href)
        items.append(_build_opportunity(cleaned_text, "Startups Gallery", href))

    return items[:limit]
