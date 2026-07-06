from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


def _build_opportunity(title: str, organization: str, url: str) -> dict[str, Any]:
    return {
        "id": f"producthunt:{url}" if url else f"producthunt:{title}",
        "source": "producthunt",
        "category": "startup",
        "title": title,
        "organization": organization,
        "url": url,
        "location": "Remote",
        "tags": ["startup", "producthunt"],
        "description": "",
        "published": datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": 0.6,
    }


def PH_scan(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://www.producthunt.com/leaderboard/daily", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
        items = []
        for match in __import__("re").finditer(r'href="([^"]+)"[^>]*>([^<]+)</a>', html):
            href, text = match.groups()
            if "producthunt.com" not in href and len(text.strip()) > 3:
                items.append(_build_opportunity(text.strip(), "Product Hunt", href))
        return items[:limit]
    except Exception:
        return []