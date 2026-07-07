from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

SOURCE = "remoteok"
URL = "https://remoteok.com/api"


def _build_opportunity(
    title: str,
    organization: str,
    url: str,
    *,
    description: str | None = None,
    location: str | None = None,
    score: float = 0.75,
) -> dict[str, Any]:
    return {
        "id": f"{SOURCE}:{url}" if url else f"{SOURCE}:{title}",
        "source": SOURCE,
        "category": "remote",
        "title": organization,  # placeholder, replaced below
        "url": url,
        "location": location or "Remote",
        "tags": ["remote", "startup"],
        "description": description or "",
        "published": datetime.now(timezone.utc).date().isoformat(),
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

    if not isinstance(payload, list):
        return []

    items: list[dict[str, Any]] = []
    for job in payload[:limit]:
        if not isinstance(job, dict):
            continue
        # The RemoteOK API sometimes puts a meta object at index 0; guard against it.
        title = job.get("position") or job.get("title") or "Remote role"
        company = job.get("company") or "RemoteOK"
        url = f"https://remoteok.com/remote-jobs/{job.get('slug') or job.get('id')}"
        opp = _build_opportunity(
            title,
            company,
            url,
            description=job.get("description") or "",
            location=job.get("location") or "Remote",
        )
        # The setter above intentionally sets a placeholder title; fix it here.
        opp["title"] = title
        opp["organization"] = company
        # RemoteOK exposes `date` as a unix timestamp in seconds for postings.
        try:
            ts = job.get("date")
            if isinstance(ts, (int, float)):
                opp["published"] = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
        items.append(opp)
    return items
