from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


def _build_opportunity(title: str, organization: str, url: str) -> dict[str, Any]:
    return {
        "id": f"startupsgallery:{url}" if url else f"startupsgallery:{title}",
        "source": "startupsgallery",
        "category": "startup",
        "title": title,
        "organization": organization,
        "url": url,
        "location": "Remote",
        "tags": ["startup", "news"],
        "description": "",
        "published": datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": 0.6,
    }


def SG_scan(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://startups.gallery/news", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
        items = []
        for match in __import__("re").finditer(r'href="([^"]+)"[^>]*>([^<]+)</a>', html):
            href, text = match.groups()
            if href.startswith("http") and len(text.strip()) > 3:
                items.append(_build_opportunity(text.strip(), "Startups Gallery", href))
        return items[:limit]
    except Exception:
        return []