"""Master LangGraph pipeline connecting all 4 nodes.

Pipeline flow: funding_detector → enrichment → scorer → save
"""

import logging
from functools import partial

import httpx
import redis.asyncio as aioredis
from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.pipeline.nodes.enrichment import enrichment_node
from app.pipeline.nodes.funding_detector import funding_detector_node
from app.pipeline.nodes.save import save_node
from app.pipeline.nodes.scorer import scorer_node
from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)


def build_pipeline(
    http_client: httpx.AsyncClient,
    redis: aioredis.Redis,
    settings: Settings,
    browser=None,
) -> StateGraph:
    """Build and compile the LangGraph pipeline with injected dependencies."""

    # Create partial functions with dependencies injected
    async def detect(state: PipelineState) -> dict:
        return await funding_detector_node(
            state,
            http_client=http_client,
            redis=redis,
            settings=settings,
            browser=browser,
        )

    async def enrich(state: PipelineState) -> dict:
        return await enrichment_node(state, http_client=http_client)

    async def score(state: PipelineState) -> dict:
        return await scorer_node(state)

    async def save(state: PipelineState) -> dict:
        return await save_node(state)

    builder = StateGraph(PipelineState)

    builder.add_node("funding_detector", detect)
    builder.add_node("enrichment", enrich)
    builder.add_node("scorer", score)
    builder.add_node("save", save)

    builder.add_edge(START, "funding_detector")
    builder.add_edge("funding_detector", "enrichment")
    builder.add_edge("enrichment", "scorer")
    builder.add_edge("scorer", "save")
    builder.add_edge("save", END)

    return builder.compile()


async def run_pipeline(
    http_client: httpx.AsyncClient,
    redis: aioredis.Redis,
    settings: Settings,
    browser=None,
) -> dict:
    """Build and execute the full pipeline. Returns final state."""
    pipeline = build_pipeline(http_client, redis, settings, browser)

    initial_state: PipelineState = {
        "raw_companies": [],
        "enriched_companies": [],
        "scored_companies": [],
        "saved_count": 0,
        "errors": [],
        "stats": {},
    }

    logger.info("Starting FundingRadar pipeline...")
    result = await pipeline.ainvoke(initial_state)
    logger.info(
        f"Pipeline complete. Stats: {result.get('stats', {})}. "
        f"Errors: {len(result.get('errors', []))}"
    )
    return result
