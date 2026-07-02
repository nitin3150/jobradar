"""Node 3: Hiring Intent Scorer.

Uses Claude API to score each company's hiring intent (0-100)
and predict likely roles they're hiring for.
"""

import json
import logging

from app.pipeline.llm import call_claude_json
from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)

SCORER_PROMPT = """Given this startup data:
{company_data}

Analyze their hiring intent and return JSON with:
- hiring_intent_score: integer 0-100 (100 = actively hiring right now)
- likely_roles: list of role titles they are probably hiring for (focus on AI/ML, engineering, and product roles)
- company_summary: 2 sentence summary of the company
- reasoning: brief explanation of why you scored it this way

Consider these signals:
- Recent funding = likely hiring
- Hiring signals from social media = strong indicator
- Larger funding amounts = more roles
- Earlier stage companies = more generalist roles
- AI/ML companies = likely need ML engineers

Return ONLY valid JSON, no other text."""

# Default fallback when Claude API fails
DEFAULT_SCORE = {
    "hiring_intent_score": 50,
    "likely_roles": ["Software Engineer", "Product Manager"],
    "company_summary": "Recently funded startup.",
    "reasoning": "Default score - LLM scoring unavailable",
}


async def scorer_node(state: PipelineState) -> dict:
    """Score each enriched company's hiring intent using Claude."""
    enriched = state.get("enriched_companies", [])
    errors = list(state.get("errors", []))
    scored = []

    for company in enriched:
        try:
            score_data = await _score_company(company)
            scored_company = {**company, **score_data}
            scored.append(scored_company)
        except Exception as e:
            logger.warning(f"Failed to score {company.get('name', '?')}: {e}")
            errors.append(f"scorer: {company.get('name', '?')}: {e}")
            scored.append({**company, **DEFAULT_SCORE})

    stats = {**state.get("stats", {}), "scored": len(scored)}
    return {"scored_companies": scored, "errors": errors, "stats": stats}


async def _score_company(company: dict) -> dict:
    """Score a single company using Claude API."""
    # Prepare a clean subset of data for the prompt
    prompt_data = {
        "name": company.get("name", "Unknown"),
        "description": company.get("description", ""),
        "funding_amount": company.get("funding_amount"),
        "funding_stage": company.get("funding_stage", "unknown"),
        "funding_date": company.get("funding_date"),
        "source": company.get("source", ""),
        "hiring_signals": company.get("hiring_signals", []),
        "team_size": company.get("team_size"),
        "website": company.get("website", ""),
    }

    prompt = SCORER_PROMPT.format(company_data=json.dumps(prompt_data, default=str))
    result = await call_claude_json(prompt, max_tokens=512)

    if result is None:
        logger.warning(f"Using default score for {company.get('name', '?')}")
        return DEFAULT_SCORE

    # Validate and clamp the score
    score = result.get("hiring_intent_score", 50)
    if not isinstance(score, (int, float)):
        score = 50
    score = max(0, min(100, int(score)))

    return {
        "hiring_intent_score": score,
        "likely_roles": result.get("likely_roles", DEFAULT_SCORE["likely_roles"]),
        "company_summary": result.get("company_summary", DEFAULT_SCORE["company_summary"]),
        "hiring_signals": company.get("hiring_signals", []),
    }
