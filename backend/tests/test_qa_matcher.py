import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4
from apply_worker.qa_matcher import find_match, normalize


def test_normalize_lowercases_and_strips():
    assert normalize("  Are you authorized to work? ") == "are you authorized to work"


def test_find_match_exact():
    entries = [
        MagicMock(id=uuid4(), question_pattern="authorized to work in the us", answer="Yes", times_used=0)
    ]
    match = find_match("Are you authorized to work in the US?", entries, threshold=0.75)
    assert match is not None
    assert match.answer == "Yes"


def test_find_match_no_match_returns_none():
    entries = [
        MagicMock(id=uuid4(), question_pattern="salary expectations", answer="120000", times_used=0)
    ]
    with patch("apply_worker.qa_matcher.llm_complete", return_value='{"match_index": -1}'):
        match = find_match("Describe your GitHub contributions", entries, threshold=0.75)
    assert match is None


def test_find_match_empty_bank_returns_none():
    match = find_match("Any question", [], threshold=0.75)
    assert match is None
