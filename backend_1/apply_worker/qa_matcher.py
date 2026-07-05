"""Two-pass Q&A bank matcher for form field labels."""
import json
import logging
import re
from typing import Any

from rapidfuzz import fuzz

from app.llm.client import llm_complete

logger = logging.getLogger(__name__)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def find_match(
    field_label: str,
    bank_entries: list[Any],
    threshold: float = 0.75,
) -> Any | None:
    """Find best matching Q&A bank entry for a form field label.

    Two-pass:
    1. rapidfuzz token_set_ratio against question_pattern
    2. LiteLLM semantic match if pass 1 score < threshold

    Returns matching QABankEntry or None.
    """
    if not bank_entries:
        return None

    normalized_label = normalize(field_label)

    # Pass 1: fuzzy keyword match
    best_entry = None
    best_score = 0.0

    for entry in bank_entries:
        score = fuzz.token_set_ratio(
            normalized_label, normalize(entry.question_pattern)
        ) / 100.0
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score >= threshold:
        logger.debug(f"Pass 1 match: '{field_label}' -> '{best_entry.question_pattern}' ({best_score:.2f})")
        return best_entry

    # Pass 2: LiteLLM semantic match
    bank_list = "\n".join(
        f"{i}: {e.question_pattern}" for i, e in enumerate(bank_entries)
    )
    prompt = f"""You are matching a job application form field label to a Q&A bank.

Form field label: "{field_label}"

Q&A bank entries (index: pattern):
{bank_list}

Which entry best matches the form field? Reply ONLY with valid JSON:
{{"match_index": <index or -1 if no match>, "confidence": <0.0-1.0>}}

Use -1 if no entry is a reasonable match (confidence < {threshold})."""

    try:
        raw = llm_complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=64,
        )
        data = json.loads(raw.strip())
        idx = int(data.get("match_index", -1))
        confidence = float(data.get("confidence", 0.0))

        if idx >= 0 and confidence >= threshold and idx < len(bank_entries):
            logger.debug(f"Pass 2 match: '{field_label}' -> index {idx} ({confidence:.2f})")
            return bank_entries[idx]
    except Exception as e:
        logger.error(f"Pass 2 matcher failed: {e}")

    return None
