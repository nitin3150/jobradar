"""Classify Gmail reply as interview / rejection / other."""
import json
import logging

from app.llm.client import llm_complete

logger = logging.getLogger(__name__)

CLASSIFY_PROMPT = """You are classifying an email reply to a job application.

Email subject: {subject}
Email snippet: {snippet}

Classify this email into one of: interview, rejection, other

"interview" = scheduling an interview, asking for availability, next steps
"rejection" = thanking for applying but not moving forward
"other" = generic acknowledgement, newsletter, unclear

Respond ONLY with valid JSON: {{"classification": "interview|rejection|other"}}"""


def classify_reply(subject: str, snippet: str) -> str:
    """Returns 'interview', 'rejection', or 'other'."""
    prompt = CLASSIFY_PROMPT.format(subject=subject, snippet=snippet[:500])
    try:
        raw = llm_complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=32,
        )
        data = json.loads(raw.strip())
        result = data.get("classification", "other")
        if result not in ("interview", "rejection", "other"):
            return "other"
        return result
    except Exception as e:
        logger.error(f"Gmail classifier failed: {e}")
        return "other"
