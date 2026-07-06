from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


def _build_opportunity(title: str, organization: str, url: str, published: str | None = None) -> dict[str, Any]:
    return {
        "id": f"hackernews:{url}" if url else f"hackernews:{title}",
        "source": "hackernews",
        "category": "startup",
        "title": title,
        "organization": organization,
        "url": url,
        "location": "Remote",
        "tags": ["startup", "hackernews"],
        "description": "",
        "published": published or datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": 0.7,
    }


def HN_scan(limit: int = 10) -> list[dict[str, Any]]:
    try:
        res = httpx.get("https://hacker-news.firebaseio.com/v0/jobstories.json", timeout=10.0, follow_redirects=True)
        res.raise_for_status()
        ids = res.json()
        if not ids:
            return []

        items = []
        for story_id in ids[:limit]:
            story_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
            story_resp = httpx.get(story_url, timeout=10.0, follow_redirects=True)
            story_resp.raise_for_status()
            info = story_resp.json()
            if not info.get("title"):
                continue
            published = datetime.fromtimestamp(info.get("time", 0), tz=timezone.utc).date().isoformat() if info.get("time") else None
            items.append(_build_opportunity(info.get("title", "Hacker News story"), info.get("by", "Hacker News"), info.get("url") or f"https://news.ycombinator.com/item?id={story_id}", published=published))
        return items
    except Exception:
        return []