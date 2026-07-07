from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

SOURCE = "remotive"
URL = "https://remotive.com/api/remote-jobs?limit=10"


def _build_opportunity(
    title: str,
    organization: str,
    url: str,
    *,
    description: str | None = None,
    location: str | None = None,
    published: str | None = None,
    score: float = 0.75,
) -> dict[str, Any]:
    return {
        "id": f"{SOURCE}:{url}" if url else f"{SOURCE}:{title}",
        "source": SOURCE,
        "category": "remote",
        "title": title,
        "organization": organization,
        "url": url,
        "location": location or "Remote",
        "tags": ["remote", "startup"],
        "description": description or "",
        "published": published or datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": score,
    }


def scan(limit: int = 30) -> list[dict[str, Any]]:
    try:
        response = httpx.get(URL, timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    for job in jobs[:limit]:
        items.append(
            _build_opportunity(
                job.get("title") or "Remote role",
                job.get("company_name") or "Remotive",
                job.get("url") or "",
                description=job.get("description") or "",
                location=job.get("candidate_required_location") or "Remote",
                published=job.get("publication_date") or None,
            )
        )
    return items
