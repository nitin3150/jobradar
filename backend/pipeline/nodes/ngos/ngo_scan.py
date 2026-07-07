"""NGO scanner for the NGOs domain.

Hits two sources today:
* ReliefWeb's public JSON API (no key needed)
* Idealist's job listings page

Both sources expose a ``title`` + ``url`` + ``organization`` triple which
fits the standardized opportunity model. Callers should apply their own
``delta_hours`` cutoff at the runner level.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx


def _build_opportunity(
    source: str,
    title: str,
    organization: str,
    url: str,
    *,
    description: str | None = None,
    location: str | None = None,
    published: str | None = None,
    tags: list[str] | None = None,
    salary: str | None = None,
    score: float = 0.0,
) -> dict[str, Any]:
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
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for entry in payload.get("data", [])[:limit]:
        fields = entry.get("fields", {})
        title = fields.get("title") or "ReliefWeb opportunity"
        organization = (
            fields.get("source", [{}])[0].get("name")
            if fields.get("source")
            else "ReliefWeb"
        )
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


def _parse_idealist_jobs(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://www.idealist.org/en/jobs", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
    except Exception:
        return []

    matches: list[tuple[str, str]] = []
    for match in re.finditer(r'href="([^"]+)"[^>]*>([^<]+)</a>', html):
        href, text = match.groups()
        if "job" in href.lower() and len(text.strip()) > 3:
            matches.append((text.strip(), href))

    items: list[dict[str, Any]] = []
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


def scan(limit: int = 20, sources: list[str] | None = None) -> list[dict[str, Any]]:
    """Run all NGO sources with optional ``sources`` allow-list."""
    wanted = {s.lower() for s in (sources or ["reliefweb", "idealist"])}
    opportunities: list[dict[str, Any]] = []
    if "reliefweb" in wanted:
        opportunities.extend(_parse_reliefweb_jobs(limit=limit))
    if "idealist" in wanted:
        opportunities.extend(_parse_idealist_jobs(limit=limit))
    return opportunities[:limit]
