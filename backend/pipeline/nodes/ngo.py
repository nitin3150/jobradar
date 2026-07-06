from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


def _build_opportunity(source: str, title: str, organization: str, url: str, *, description: str | None = None, location: str | None = None, published: str | None = None, tags: list[str] | None = None, salary: str | None = None, score: float = 0.0) -> dict[str, Any]:
    return {
        "id": f"{source}:{url}" if url else f"{source}:{title}",
        "source": source,
        "category": "ngo",
        "title": title,
        "organization": organization,
        "url": url,
        "location": location or "Unknown",
        "tags": tags or [],
        "description": description or "",
        "published": published or datetime.now(timezone.utc).date().isoformat(),
        "salary": salary,
        "status": "review",
        "score": score,
    }


def _parse_reliefweb_jobs(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://api.reliefweb.int/v1/jobs?limit=10", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        items = []
        for entry in payload.get("data", [])[:limit]:
            fields = entry.get("fields", {})
            title = fields.get("title") or "ReliefWeb opportunity"
            organization = fields.get("source", [{}])[0].get("name") if fields.get("source") else "ReliefWeb"
            url = fields.get("url") or entry.get("href")
            items.append(
                _build_opportunity(
                    "reliefweb",
                    title,
                    organization,
                    url or "",
                    description=fields.get("description") or "",
                    location=fields.get("country") or fields.get("city") or "Remote",
                    published=fields.get("date") or None,
                    tags=["ngo", "humanitarian"],
                    score=0.8,
                )
            )
        return items
    except Exception:
        return []


def _parse_idealist_jobs(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://www.idealist.org/en/jobs", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
        matches = []
        for match in __import__("re").finditer(r'href="([^"]+)"[^>]*>([^<]+)</a>', html):
            href, text = match.groups()
            if "job" in href.lower() and len(text.strip()) > 3:
                matches.append((text.strip(), href))
        items = []
        for title, href in matches[:limit]:
            items.append(
                _build_opportunity(
                    "idealist",
                    title,
                    "Idealist",
                    f"https://www.idealist.org{href}" if href.startswith("/") else href,
                    tags=["ngo", "volunteering"],
                    score=0.7,
                )
            )
        return items
    except Exception:
        return []


def scan_ngos(state: dict[str, Any] | None = None, limit: int = 20) -> dict[str, Any]:
    opportunities = []
    opportunities.extend(_parse_reliefweb_jobs(limit=limit))
    opportunities.extend(_parse_idealist_jobs(limit=limit))

    seen = set()
    unique = []
    for item in opportunities:
        key = item["url"] or item["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return {**(state or {}), "opportunities": unique[:limit], "res": unique[:limit]}
