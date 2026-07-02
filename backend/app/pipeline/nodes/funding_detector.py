"""Node 1: Funding Detector.

Runs all enabled scrapers in parallel and collects raw company data.
Deduplication happens at the scraper level via Redis.
"""

import logging

from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)


async def funding_detector_node(
    state: PipelineState,
    http_client=None,
    redis=None,
    settings=None,
    browser=None,
) -> dict:
    """Run all scrapers and return raw companies."""
    from app.scrapers import run_all_scrapers

    errors = list(state.get("errors", []))

    try:
        raw_companies = await run_all_scrapers(
            http_client=http_client,
            redis=redis,
            settings=settings,
            browser=browser,
        )
    except Exception as e:
        logger.error(f"Funding detector failed: {e}")
        errors.append(f"funding_detector: {e}")
        raw_companies = []

    return {
        "raw_companies": raw_companies,
        "errors": errors,
        "stats": {"detected": len(raw_companies)},
    }
