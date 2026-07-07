"""HackerNews "Who is hiring" stories — these are actual job postings, so they
belong under the Remote Jobs domain rather than Funding News.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

SOURCE = "hackernews"


def _build_opportunity(title: str, organization: str, url: str, published: str | None = None) -> dict[str, Any]:
    return {
        "id": f"{SOURCE}:{url}" if url else f"{SOURCE}:{title}",
        "source": SOURCE,
        "category": "remote",
        "title": title,
        "organization": organization,
        "url": url,
        "location": "Remote",
        "tags": ["remote", "hackernews"],
        "description": "",
        "published": published or datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": 0.7,
    }


def scan(limit: int = 30) -> list[dict[str, Any]]:
    try:
        res = httpx.get("https://hacker-news.firebaseio.com/v0/jobstories.json", timeout=10.0, follow_redirects=True)
        res.raise_for_status()
        ids = res.json() or []
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for story_id in ids[:limit]:
        try:
            story_resp = httpx.get(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                timeout=10.0,
                follow_redirects=True,
            )
            story_resp.raise_for_status()
            info = story_resp.json()
        except Exception:
            continue
        if not info.get("title"):
            continue
        published = (
            datetime.fromtimestamp(info.get("time", 0), tz=timezone.utc).date().isoformat()
            if info.get("time")
            else None
        )
        items.append(
            _build_opportunity(
                info.get("title", "Hacker News story"),
                info.get("by", "Hacker News"),
                info.get("url") or f"https://news.ycombinator.com/item?id={story_id}",
                published=published,
            )
        )
    return items
