"""
JobScanner — Zero‑token job board scanner for JobRadar.

Replicates the career‑ops `scan.mjs` functionality in Python,
with an added `--hours` filter for recent postings.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

from scanner.providers.api_scraper import fetch_api
from scanner.providers.base import Job
from scanner.providers.local_parser import fetch_local_parser
from scanner.providers.playwright_scraper import fetch_playwright
from scanner.providers.websearch_scraper import fetch_websearch
from scanner.utils.tsv_io import (
    append_to_history,
    append_to_pipeline,
    ensure_pipeline_section,
    load_history,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).parent / "config" / "portals.yml"
SCAN_HISTORY_PATH = Path(__file__).parent / "data" / "scan-history.tsv"
PIPELINE_PATH = Path(__file__).parent / "data" / "pipeline.md"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Filtering helpers (mirrors career‑ops filters)
# ---------------------------------------------------------------------------
def build_title_filter(title_cfg: Dict) -> callable:
    positive = [k.lower() for k in title_cfg.get("positive", [])]
    negative = [k.lower() for k in title_cfg.get("negative", [])]

    def _filter(title: str) -> bool:
        t = title.lower()
        if negative and any(k in t for k in negative):
            return False
        if positive:
            return any(k in t for k in positive)
        return True

    return _filter


def build_location_filter(loc_cfg: Dict) -> callable:
    always_allow = [k.lower() for k in loc_cfg.get("always_allow", [])]
    allow = [k.lower() for k in loc_cfg.get("allow", [])]
    block = [k.lower() for k in loc_cfg.get("block", [])]

    def _filter(location: str) -> bool:
        if not location:
            return True
        l = location.lower()
        if always_allow and any(k in l for k in always_allow):
            return True
        if block and any(k in l for k in block):
            return False
        if allow:
            return any(k in l for k in allow)
        return True

    return _filter


def build_content_filter(content_cfg: Dict) -> callable:
    positive = [k.lower() for k in content_cfg.get("positive", [])]
    negative = [k.lower() for k in content_cfg.get("negative", [])]

    def _filter(description: str) -> bool:
        if not isinstance(description, str):
            return True
        d = description.lower()
        if negative and any(k in d for k in negative):
            return False
        if positive:
            return any(k in d for k in positive)
        return True

    return _filter


def build_salary_filter(salary_cfg: Dict) -> callable:
    min_val = salary_cfg.get("min", 0) or 0
    max_val = salary_cfg.get("max", 0) or 0
    currency = salary_cfg.get("currency", "").upper()

    def _filter(salary: Any) -> bool:
        return True  # Placeholder — implement actual salary parsing if needed

    return _filter


# ---------------------------------------------------------------------------
# Date filter (new feature: --hours)
# ---------------------------------------------------------------------------
def filter_by_date(jobs: List[Job], hours: float) -> List[Job]:
    """Keep only jobs posted within the last N hours."""
    if hours <= 0:
        return jobs
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = []
    for job in jobs:
        if job.posted_at and job.posted_at >= cutoff:
            filtered.append(job)
    logger.info(f"Date filter ({hours}h): {len(filtered)}/{len(jobs)} jobs kept")
    return filtered


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def load_seen() -> Tuple[Set[str], Set[str]]:
    rows = load_history()
    seen_urls = {r["url"] for r in rows}
    seen_combos = {
        f"{r['company'].lower().strip()}::{r['title'].lower().strip()}"
        for r in rows
        if r.get("status") not in ("skipped_expired",)
    }
    return seen_urls, seen_combos


# ---------------------------------------------------------------------------
# Discovery levels
# ---------------------------------------------------------------------------
async def run_level0_local_parser(entries: List[Dict]) -> List[Job]:
    jobs = []
    for entry in entries:
        if entry.get("parser"):
            try:
                jobs.extend(await fetch_local_parser(entry))
            except Exception as e:
                logger.error(f"Local parser failed for {entry.get('name')}: {e}")
    return jobs


async def run_level1_playwright(entries: List[Dict]) -> List[Job]:
    jobs = []
    for entry in entries:
        if entry.get("careers_url"):
            try:
                jobs.extend(await fetch_playwright(entry))
            except Exception as e:
                logger.error(f"Playwright failed for {entry.get('name')}: {e}")
    return jobs


async def run_level2_api(entries: List[Dict]) -> List[Job]:
    jobs = []
    for entry in entries:
        if entry.get("ats_type"):
            try:
                jobs.extend(await fetch_api(entry))
            except Exception as e:
                logger.error(f"API fetch failed for {entry.get('name')}: {e}")
    return jobs


async def run_level3_websearch(entries: List[Dict], queries: List[Dict]) -> List[Job]:
    jobs = []
    for entry in entries:
        if entry.get("enabled", True) and entry.get("company") != "GENERIC一分":
            try:
                jobs.extend(await fetch_websearch(entry, queries))
            except Exception as e:
                logger.error(f"Web search failed for {entry.get('name')}: {e}")
    return jobs


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------
async def scan(
    config_path: Path = DEFAULT_CONFIG,
    *,
    dry_run: bool = False,
    verify: bool = False,
    company_filter: str = "",
    hours: float = 0,
) -> List[Job]:
    """
    Run the full scan across all configured sources.

    Returns the list of *new* jobs that passed all filters.
    """
    # 1️⃣ Load config
    config = load_config(config_path)
    tracked = [c for c in config.get("tracked_companies", []) if c.get("enabled", True)]
    queries = [q for q in config.get("search_queries", []) if q.get("enabled", True)]
    filter_cfg = config.get("filters", {})

    # Optional company filter
    if company_filter:
        tracked = [c for c in tracked if company_filter.lower() in c.get("name", "").lower()]

    # 2️⃣ Load history for dedup
    seen_urls, seen_combos = load_seen()

    # 3️⃣ Run discovery levels (0 → 1 → 2 → 3)
    logger.info("Starting job scan …")
    all_jobs: List[Job] = []

    # Level 0: Local parsers
    local_jobs = await run_level0_local_parser(tracked)
    all_jobs.extend(local_jobs)
    logger.info(f"Level 0 (local parser): {len(local_jobs)} jobs")

    # Level 1: Playwright (direct careers pages)
    # Skip if local parser succeeded for that company
    local_ok = {c["name"] for c in tracked if c.get("parser")}
    playwright_entries = [c for c in tracked if c["name"] not in local_ok]
    pw_jobs = await run_level1_playwright(playwright_entries)
    all_jobs.extend(pw_jobs)
    logger.info(f"Level 1 (playwright): {len(pw_jobs)} jobs")

    # Level 2: ATS APIs
    api_entries = [c for c in tracked if c["name"] not in local_ok]
    api_jobs = await run_level2_api(api_entries)
    all_jobs.extend(api_jobs)
    logger.info(f"Level 2 (API): {len(api_jobs)} jobs")

    # Level 3: Web search (broad discovery)
    if queries:
        ws_jobs = await run_level3_websearch(tracked, queries)
        all_jobs.extend(ws_jobs)
        logger.info(f"Level 3 (websearch): {len(ws_jobs)} jobs")

    # 4️⃣ Apply filters
    title_filter = build_title_filter(filter_cfg.get("title_filter", {}))
    loc_filter = build_location_filter(filter_cfg.get("location_filter", {}))
    content_filter = build_content_filter(filter_cfg.get("content_filter", {}))
    salary_filter = build_salary_filter(filter_cfg.get("salary_filter", {}))

    filtered: List[Job] = []
    for job in all_jobs:
        if not title_filter(job.title):
            continue
        if not loc_filter(job.location):
            continue
        if not content_filter(job.description):
            continue
        if not salary_filter(job.salary):
            continue
        filtered.append(job)

    logger.info(f"After filters: {len(filtered)}/{len(all_jobs)} jobs")

    # 5️⃣ Date filter (new feature)
    filtered = filter_by_date(filtered, hours)

    # 6️⃣ Deduplicate
    new_jobs = []
    for job in filtered:
        if job.url in seen_urls:
            continue
        combo = f"{job.company.lower().strip()}::{job.title.lower().strip()}"
        if combo in seen_combos:
            continue
        new_jobs.append(job)
        seen_urls.add(job.url)
        seen_combos.add(combo)

    logger.info(f"After dedup: {len(new_jobs)} new jobs")

    # 7️⃣ Optional liveness verification
    if verify:
        from playwright.async_api import async_playwright

        verified = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for job in new_jobs:
                try:
                    page = await browser.new_page()
                    resp = await page.goto(job.url, timeout=15000)
                    if resp and resp.status < 400:
                        text = await page.inner_text("body")
                        if any(
                            kw in text.lower()
                            for kw in ("apply", "apply now", "submit")
                        ):
                            verified.append(job)
                    await page.close()
                except Exception:
                    continue
            await browser.close()
        new_jobs = verified
        logger.info(f"After verification: {len(new_jobs)} jobs")

    # 8️⃣ Persist results
    if not dry_run and new_jobs:
        ensure_pipeline_section()
        append_to_pipeline([j.__dict__ for j in new_jobs])
        append_to_history([j.__dict__ for j in new_jobs], status="added")
        logger.info(f"Saved {len(new_jobs)} jobs to pipeline & history")
    elif dry_run:
        logger.info("Dry-run mode: no files written")

    return new_jobs


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JobRadar Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--verify", action="store_true", help="Verify job pages are live")
    parser.add_argument("--company", default="", help="Filter to a specific company")
    parser.add_argument(
        "--hours",
        type=float,
        default=0,
        help="Only keep jobs posted within the last N hours (0 = no filter)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to portals.yml")
    return parser.parse_args()


async def main_async():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    jobs = await scan(
        config_path=args.config,
        dry_run=args.dry_run,
        verify=args.verify,
        company_filter=args.company,
        hours=args.hours,
    )

    print(f"\n{'=' * 45}")
    print(f"JobRadar Scanner — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"{'=' * 45}")
    print(f"New jobs found: {len(jobs)}")
    for j in jobs:
        print(f"  + {j.company} | {j.title} | {j.location or 'N/A'}")
    if not args.dry_run:
        print(f"\nResults saved to {PIPELINE_PATH} and {SCAN_HISTORY_PATH}")
    print("\n→ Run `/career-ops pipeline` to evaluate new offers.")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
