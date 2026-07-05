"""Tests for the LangGraph pipeline nodes."""

import json

import json

import pytest
from unittest.mock import AsyncMock, patch

from app.pipeline.nodes.scorer import scorer_node, DEFAULT_SCORE


@pytest.mark.asyncio
async def test_scorer_node_with_fallback():
    """Test that scorer node returns default score when LLM fails."""
    state = {
        "enriched_companies": [
            {
                "name": "Test Co",
                "description": "A test company",
                "funding_stage": "seed",
                "source": "test",
            }
        ],
        "errors": [],
        "stats": {},
    }

    # After LLM-client consolidation, scorer.py calls llm_complete directly
    # and parses JSON inline. The mock raises so the except-branch returns
    # DEFAULT_SCORE — the old `return_value=None` path no longer exists.
    with patch(
        "app.pipeline.nodes.scorer.llm_complete",
        side_effect=Exception("API offline"),
    ):
        result = await scorer_node(state)

    assert len(result["scored_companies"]) == 1
    company = result["scored_companies"][0]
    assert company["hiring_intent_score"] == DEFAULT_SCORE["hiring_intent_score"]
    assert company["likely_roles"] == DEFAULT_SCORE["likely_roles"]


@pytest.mark.asyncio
async def test_scorer_node_with_llm_response():
    """Test that scorer node correctly applies LLM scores."""
    state = {
        "enriched_companies": [
            {
                "name": "AI Startup",
                "description": "Building AI tools",
                "funding_stage": "series-a",
                "funding_amount": 10_000_000,
                "source": "sec_edgar",
                "hiring_signals": ["We're hiring ML engineers!"],
            }
        ],
        "errors": [],
        "stats": {},
    }

    mock_response = {
        "hiring_intent_score": 85,
        "likely_roles": ["ML Engineer", "Backend Engineer", "Product Manager"],
        "company_summary": "AI Startup is building cutting-edge AI tools. Recently raised Series A.",
        "reasoning": "High score due to active hiring signals and recent Series A.",
    }

    # The mock now returns a JSON STRING (the raw `llm_complete` content),
    # because scorer.py does inline `json.loads(...)` on the response.
    with patch(
        "app.pipeline.nodes.scorer.llm_complete",
        return_value=json.dumps(mock_response),
    ):
        result = await scorer_node(state)

    assert len(result["scored_companies"]) == 1
    company = result["scored_companies"][0]
    assert company["hiring_intent_score"] == 85
    assert "ML Engineer" in company["likely_roles"]
