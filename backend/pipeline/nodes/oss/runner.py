"""Open Source domain runner (5th tab in the navbar).

Combines the GitHub Trending HTML scraper with the GitHub Search-API
good-first-issues scraper, runs the rule-based strategy generator on
every row, applies a ``delta_hours`` cutoff against ``last_activity``,
and emits the standardized opportunity shape with all extensions.

By design, *Trending* + *Good-First-Issues* can overlap on the same
repo. We merge by URL: if both sources have a row for
``owner/repo`` we keep the trending row (richer base) and attach the
GFI-sourced ``top_issues`` list to it. This gives every cell the more
useful metadata while preserving the action surface for maintainer
outreach.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from utils.time_check import parse_opportunity_published

from .github_issues import scan as gfi_scan
from .github_trending import scan as trending_scan
from .strategy import attach_strategy


def _normalize(value: str) -> str:
    return value.strip().lower().rstrip("/")


def _merge_opportunities(
    trending: list[dict[str, Any]],
    good_first_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge by repo URL; trending rows absorb GFI-sourced ``top_issues``.

    Copies each trending row before mutating it so subsequent callers of
    ``trending_scan`` (e.g. unit tests, repeat pipeline runs in the same
    process) don't see leftover ``top_issues`` fields on rows they never
    asked for.
    """
    gfi_by_repo: dict[str, dict[str, Any]] = {}
    for opp in good_first_issues:
        gfi_by_repo[_normalize(opp["url"])] = opp

    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for opp in trending:
        url = _normalize(opp["url"])
        seen_urls.add(url)
        merged_row = dict(opp)
        gfi_row = gfi_by_repo.get(url)
        if gfi_row and gfi_row.get("top_issues"):
            merged_row["top_issues"] = gfi_row["top_issues"]
        merged.append(merged_row)

    # Include GFI-only repos that didn't appear in trending.
    for opp in good_first_issues:
        url = _normalize(opp["url"])
        if url in seen_urls:
            continue
        merged.append(opp)

    return merged


def scan_oss(
    delta_hours: int = 168,
    limit: int = 50,
    sources: list[str] | None = None,
    languages: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the Open Source domain with a ``delta_hours`` cutoff.

    ``sources`` accepts ``"github_trending"`` and ``"github_issues"``.
    ``languages`` is the language list passed to BOTH scrapers.
    """
    wanted_sources = {s.lower() for s in (sources or ["github_trending", "github_issues"])}
    wanted_languages = [l.lower() for l in (languages or ["python", "typescript", "go"])]

    trending: list[dict[str, Any]] = []
    if "github_trending" in wanted_sources or "github" in wanted_sources:
        for lang in wanted_languages:
            trending.extend(trending_scan(limit=limit, language=lang))

    good_first_issues: list[dict[str, Any]] = []
    if "github_issues" in wanted_sources or "github" in wanted_sources:
        for lang in wanted_languages:
            good_first_issues.extend(gfi_scan(limit=limit, language=lang))

    candidates = _merge_opportunities(trending, good_first_issues)

    # Apply delta_hours cutoff on ``last_activity`` (or ``published`` as fallback).
    cutoff = datetime.now(timezone.utc) - timedelta(hours=delta_hours)

    enriched: list[dict[str, Any]] = []
    for opp in candidates:
        last_activity = opp.get("last_activity")
        parsed = parse_opportunity_published(last_activity) if last_activity else None
        if parsed is None:
            parsed = parse_opportunity_published(opp.get("published"))
        if parsed is not None and parsed < cutoff:
            continue
        enriched.append(attach_strategy(opp))

    # Stable, highest-score first, then alphabetical for ties.
    enriched.sort(key=lambda o: (-float(o.get("score") or 0.0), str(o.get("title") or "")))
    return enriched[:limit]
