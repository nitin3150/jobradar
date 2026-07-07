"""NGOs domain runner.

Wraps the per-source NGO scrapers and applies a ``delta_hours`` cutoff so
a single request can ask for *job openings from the last N hours*.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from utils.time_check import parse_opportunity_published

from .ngo_scan import scan as ngo_scan


def scan_ngos(
    delta_hours: int = 72,  # default 3 days for NGO listings (they post slowly)
    limit: int = 50,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the NGOs domain with a ``delta_hours`` cutoff."""
    opportunities = ngo_scan(limit=limit, sources=sources)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=delta_hours)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for opp in opportunities:
        parsed = parse_opportunity_published(opp.get("published"))
        # Unknown publish date -> assume recent (don't drop an org role for a missing timestamp).
        if parsed is not None and parsed < cutoff:
            continue
        key = opp["url"] or opp["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(opp)

    return unique[:limit]
