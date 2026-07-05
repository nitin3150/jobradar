"""ATS slug discovery — find new company job-board slugs via site: search.

Isolated from the hourly fetch loop (runs daily). It grows the set of
ATS-enabled companies; the fetch loop (run_job_scrape_pipeline) then polls
their boards on the user's schedule. Slugs are stable, so we discover once
and reuse — we do NOT re-search every hour.
"""
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.company import Company
from app.scrapers.jobs.search import search_urls

logger = logging.getLogger(__name__)

# Domains searched per board (used to build `site:` queries).
BOARD_SEARCH_DOMAINS = {
    "ashby": ["jobs.ashbyhq.com"],
    "greenhouse": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
    "lever": ["jobs.lever.co"],
}

# Regexes that pull the board slug out of a result URL, per board.
_SLUG_PATTERNS = {
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._-]*)", re.I)],
    "greenhouse": [
        re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board\?for=([A-Za-z0-9._-]+)", re.I),
        re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9][A-Za-z0-9._-]*)", re.I),
    ],
    "lever": [re.compile(r"jobs\.lever\.co/([A-Za-z0-9][A-Za-z0-9._-]*)", re.I)],
}

# First-path segments that are never a company slug.
_RESERVED = {"embed", "job_board", "jobs", "careers", "api", "static", "assets", "_next"}


def extract_slugs(ats_type: str, urls: list[str]) -> set[str]:
    """Extract candidate company slugs for a board from result URLs."""
    patterns = _SLUG_PATTERNS.get(ats_type, [])
    slugs: set[str] = set()
    for url in urls:
        for pat in patterns:
            m = pat.search(url)
            if m:
                slug = m.group(1).strip("/").lower()
                if slug and slug not in _RESERVED and not slug.startswith("_"):
                    slugs.add(slug)
                break
    return slugs


async def discover_slugs(
    http_client: httpx.AsyncClient,
    browser=None,
) -> dict[str, set[str]]:
    """Run all (board x role) searches, return {ats_type: {slugs}}."""
    found: dict[str, set[str]] = {}
    for board in settings.discovery_boards:
        domains = BOARD_SEARCH_DOMAINS.get(board)
        if not domains:
            logger.warning(f"No search domains configured for board {board!r}")
            continue
        board_slugs: set[str] = set()
        for role in settings.target_roles:
            for domain in domains:
                query = f'"{role}" site:{domain}'
                urls = await search_urls(
                    query,
                    freshness_hours=settings.discovery_freshness_hours,
                    backend=settings.discovery_search_backend,
                    browser=browser,
                    http_client=http_client,
                    serper_key=settings.serper_api_key,
                    max_results=settings.discovery_max_results,
                )
                board_slugs |= extract_slugs(board, urls)
        logger.info(f"Discovery found {len(board_slugs)} candidate {board} slugs")
        found[board] = board_slugs
    return found


async def run_discovery_pipeline(
    db: AsyncSession,
    http_client: httpx.AsyncClient,
    browser=None,
) -> int:
    """Discover slugs and attach them as ATS-enabled companies.

    Returns count of newly attached company slugs. Existing (ats_type, ats_slug)
    pairs are skipped, so this is safe to run repeatedly.
    """
    if not settings.discovery_enabled:
        logger.info("Discovery disabled, skipping")
        return 0

    found = await discover_slugs(http_client, browser)
    attached = 0

    for ats_type, slugs in found.items():
        for slug in slugs:
            # Skip if this exact board slug is already tracked.
            existing = await db.execute(
                select(Company).where(
                    Company.ats_type == ats_type, Company.ats_slug == slug
                )
            )
            if existing.scalar_one_or_none():
                continue

            name_slug = f"{ats_type}-{slug}"
            # Guard the unique name_slug constraint.
            clash = await db.execute(
                select(Company).where(Company.name_slug == name_slug)
            )
            if clash.scalar_one_or_none():
                continue

            db.add(
                Company(
                    name=slug,
                    name_slug=name_slug,
                    source="ats_discovery",
                    ats_type=ats_type,
                    ats_slug=slug,
                    category="startup",
                )
            )
            attached += 1

    await db.commit()
    logger.info(f"Discovery attached {attached} new ATS company slugs")
    return attached
