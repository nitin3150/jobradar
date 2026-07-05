"""LangGraph pipeline state definition."""

from typing import TypedDict


class PipelineState(TypedDict, total=False):
    # Raw companies from scrapers
    raw_companies: list[dict]
    # After enrichment with website data, founder info, hiring signals
    enriched_companies: list[dict]
    # After scoring with Claude
    scored_companies: list[dict]
    # Count of companies saved to DB
    saved_count: int
    # Error messages accumulated during pipeline
    errors: list[str]
    # Pipeline run stats
    stats: dict
