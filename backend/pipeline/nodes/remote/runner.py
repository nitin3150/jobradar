"""Remote Jobs domain runner.

Combines HackerNews "Who is hiring" stories with the Remotive + RemoteOK
remote-job-portal APIs and applies a ``delta_hours`` cutoff filtering so a
single request can ask for *remote jobs posted in the last N hours*.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from utils.time_check import parse_opportunity_published

from .hackernews import scan as hn_scan
from .remotive import scan as remotive_scan
from .remoteok import scan as remoteok_scan


def scan_remote(
    delta_hours: int = 24,
    limit: int = 50,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the Remote Jobs domain with a ``delta_hours`` cutoff."""
    wanted = {s.lower() for s in (sources or ["hackernews", "remotive", "remoteok"])}

    opportunities: list[dict[str, Any]] = []
    if "hackernews" in wanted or "hn" in wanted:
        opportunities.extend(hn_scan(limit=limit))
    if "remotive" in wanted:
        opportunities.extend(remotive_scan(limit=limit))
    if "remoteok" in wanted:
        opportunities.extend(remoteok_scan(limit=limit))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=delta_hours)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for opp in opportunities:
        parsed = parse_opportunity_published(opp.get("published"))
        # Unknown publish date -> assume recent (RemoteOK often doesn't publish stamps).
        if parsed is not None and parsed < cutoff:
            continue
        key = opp["url"] or opp["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(opp)

    return unique[:limit]
