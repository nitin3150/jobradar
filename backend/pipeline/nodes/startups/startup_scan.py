from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .hackernews import HN_scan
from .producthunt import PH_scan
from .startupgallary import SG_scan


def _build_opportunity(source: str, title: str, organization: str, url: str, *, description: str | None = None, location: str | None = None, published: str | None = None, tags: list[str] | None = None, salary: str | None = None, score: float = 0.0) -> dict[str, Any]:
    return {
        "id": f"{source}:{url}" if url else f"{source}:{title}",
        "source": source,
        "category": "startup",
        "title": title,
        "organization": organization,
        "url": url,
        "location": location or "Remote",
        "tags": tags or [],
        "description": description or "",
        "published": published or datetime.now(timezone.utc).date().isoformat(),
        "salary": salary,
        "status": "review",
        "score": score,
    }


def _parse_remotive(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://remotive.com/api/remote-jobs?limit=10", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        items = []
        for job in jobs[:limit]:
            items.append(
                _build_opportunity(
                    "remotive",
                    job.get("title") or "Remote role",
                    job.get("company_name") or "Remotive",
                    job.get("url") or "",
                    description=job.get("description") or "",
                    location=job.get("candidate_required_location") or "Remote",
                    published=job.get("publication_date") or None,
                    tags=["startup", "remote"],
                    score=0.75,
                )
            )
        return items
    except Exception:
        return []


def _parse_remoteok(limit: int = 10) -> list[dict[str, Any]]:
    try:
        response = httpx.get("https://remoteok.com/api", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        items = []
        for job in payload[:limit]:
            if not isinstance(job, dict):
                continue
            items.append(
                _build_opportunity(
                    "remoteok",
                    job.get("position") or job.get("title") or "Remote role",
                    job.get("company") or "RemoteOK",
                    f"https://remoteok.com/remote-jobs/{job.get('slug') or job.get('id')}",
                    description=job.get("description") or "",
                    location=job.get("location") or "Remote",
                    tags=["startup", "remote"],
                    score=0.75,
                )
            )
        return items
    except Exception:
        return []


def scan_startups(state: dict[str, Any] | None = None, limit: int = 20) -> dict[str, Any]:
    opportunities = []
    opportunities.extend(HN_scan(limit=limit))
    opportunities.extend(_parse_remotive(limit=limit))
    opportunities.extend(_parse_remoteok(limit=limit))
    opportunities.extend(PH_scan(limit=limit))
    opportunities.extend(SG_scan(limit=limit))

    seen = set()
    unique = []
    for item in opportunities:
        key = item["url"] or item["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return {**(state or {}), "opportunities": unique[:limit], "res": unique[:limit]}
