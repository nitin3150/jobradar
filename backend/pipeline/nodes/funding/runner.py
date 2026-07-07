"""Funding News domain runner.

Combines ProductHunt + StartupsGallery scrapers and applies a
``delta_hours`` cutoff using the shared ``utils.time_check`` helper, so a
single request can ask for *news from the last N hours*.

The opportunity model matches every other domain:
``{id, source, category, title, organization, url, location, tags,
  description, published, salary, status, score}``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from utils.time_check import parse_opportunity_published

from .producthunt import scan as ph_scan
from .startupgallary import scan as sg_scan


def scan_funding(
    delta_hours: int = 168,  # default 1 week for funding news (sparse updates)
    limit: int = 50,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the Funding News domain with a ``delta_hours`` cutoff.

    ``delta_hours=24`` is the canonical "last 24 hours" the user asked for;
    the default is wider because funding news has lower update frequency.
    """
    wanted = {s.lower() for s in (sources or ["producthunt", "startupsgallery"])}
    opportunities: list[dict[str, Any]] = []
    if "producthunt" in wanted:
        opportunities.extend(ph_scan(limit=limit))
    if "startupsgallery" in wanted or "startups_gallery" in wanted:
        opportunities.extend(sg_scan(limit=limit))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=delta_hours)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for opp in opportunities:
        parsed = parse_opportunity_published(opp.get("published"))
        # Unknown publish date -> assume recent (don't drop funding news for missing timestamps).
        if parsed is not None and parsed < cutoff:
            continue
        key = opp["url"] or opp["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(opp)

    return unique[:limit]
