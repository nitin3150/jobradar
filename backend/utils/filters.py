import re
from typing import Iterable

DEFAULT_IRRELEVANT_PATTERNS = [
    r"(?i)\b(sales|account executive|business development|customer success|support|operations|finance|hr|human resources|recruiter|marketing|content|design|product manager|product designer|intern|internship|contract|temporary|part-time)\b",
]

DEFAULT_RELEVANT_PATTERNS = [
    r"(?i)\b(software engineer|software engineering|engineer|developer|data engineer|machine learning|ml engineer|platform engineer|backend engineer|frontend engineer|full stack engineer|ai engineer|research engineer|sre|devops|security engineer|infrastructure engineer|site reliability|solutions engineer|applied scientist)\b",
]

NO_SPONSORSHIP_PATTERNS = [
    r"(?i)\b(no visa sponsorship|no sponsorship|not eligible for sponsorship|sponsorship not available|cannot sponsor|will not sponsor|no work authorization|citizenship required|must be a us citizen)\b",
]


def is_relevant_role(title: str, extra_patterns: Iterable[str] | None = None, extra_relevant_patterns: Iterable[str] | None = None, description: str | None = None) -> bool:
    if not title:
        return False

    lowered_title = title.lower()
    lowered_description = (description or "").lower()

    text_to_check = f"{lowered_title} {lowered_description}"

    for pattern in NO_SPONSORSHIP_PATTERNS:
        if re.search(pattern, text_to_check):
            return False

    relevant_patterns = list(extra_relevant_patterns or []) + DEFAULT_RELEVANT_PATTERNS
    if any(re.search(pattern, lowered_title) for pattern in relevant_patterns):
        return True

    for pattern in list(extra_patterns or []) + DEFAULT_IRRELEVANT_PATTERNS:
        if re.search(pattern, lowered_title):
            return False

    return False


def filter_roles(jobs: list[dict], extra_patterns: Iterable[str] | None = None, extra_relevant_patterns: Iterable[str] | None = None) -> list[dict]:
    return [
        job
        for job in jobs
        if is_relevant_role(
            job.get("title", ""),
            extra_patterns=extra_patterns,
            extra_relevant_patterns=extra_relevant_patterns,
            description=job.get("description") or job.get("content") or "",
        )
    ]
