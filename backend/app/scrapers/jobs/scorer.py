"""LiteLLM-based job fit scorer."""
import json
import logging

from app.llm.client import llm_complete

logger = logging.getLogger(__name__)

CANDIDATE_PROFILE = """
Name: Nitin | MS AI, Northeastern | Based in Boston
Target: AI Engineer / LLM Engineer / ML Engineer at Series A-C startups
Stack: LangGraph, LangChain, FastAPI, Python, React Native, AWS, Docker, MongoDB
Wants: Healthcare AI, agentic systems, LLM infra companies — full AI stack ownership
Hard pass: pure frontend, pure data analyst, 10+ yrs required, legacy enterprise tech
"""

SCORE_PROMPT = """You are evaluating a job posting for a candidate.

Candidate profile:
{profile}

Job title: {title}
Job description:
{jd_text}

Rate the fit on a scale of 0.0 to 1.0 and provide a one-sentence reasoning.
Respond ONLY with valid JSON in this exact format:
{{"score": 0.85, "reasoning": "Strong match because..."}}
"""


def score_job(title: str, jd_text: str) -> tuple[float, str]:
    """Score a job posting against candidate profile.

    Returns (score: float 0-1, reasoning: str).
    """
    prompt = SCORE_PROMPT.format(
        profile=CANDIDATE_PROFILE,
        title=title,
        jd_text=jd_text[:3000],
    )
    try:
        raw = llm_complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=128,
        )
        data = json.loads(raw.strip())
        score = float(data.get("score", 0.0))
        reasoning = str(data.get("reasoning", ""))
        return max(0.0, min(1.0, score)), reasoning
    except Exception as e:
        logger.error(f"Fit scorer failed: {e}")
        return 0.0, ""
