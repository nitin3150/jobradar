"""Tests for ``apply_worker.qa_matcher.match_questions``.

Covers:

* Empty inputs — ``fields=[]`` short-circuits without firing the LLM.
* Pass 1 — rapidfuzz match wins on a clear pattern match; ``source="rapidfuzz"``.
* Pass 1 — non-text fields (``file``, ``submit``) filtered out
  before the rapidfuzz pass.
* ``skip_no_answer_entries=True`` (default) — blank bank entries
  with ``answer=None`` are filtered from BOTH passes.
* Pass 2 — LLM batch picks up fields that rapidfuzz couldn't;
  ``source="llm"`` on the result rows; ``run_json_prompt`` is
  awaited exactly once for the whole batch.
* Pass 2 — LLM error collapses all unmatched fields to
  ``MATCH_SOURCE_NONE`` rather than aborting.
* Pass 2 — LLM hallucinated bank id collapses the affected field
  to ``MATCH_SOURCE_NONE`` rather than matching blindly.
* Order preservation — ``match_questions`` returns results in
  the same order as ``fields``.

The ``llm_client`` AsyncMock is awaited via ``asyncio.run`` —
each test only awaits once and the explicit body keeps the
test surface readable for contributors who haven't seen
pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from apply_worker.qa_matcher import (
    DEFAULT_LLM_THRESHOLD,
    DEFAULT_RAPIDFUZZ_THRESHOLD,
    match_questions,
)
from apply_worker.types import (
    MATCH_SOURCE_LLM,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_RAPIDFUZZ,
    FormFieldRecord,
    QABankRecord,
)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------
# Constants — pin against accidental threshold drift
# --------------------------------------------------------------------


def test_default_rapidfuzz_threshold_is_85():
    assert DEFAULT_RAPIDFUZZ_THRESHOLD == 85


def test_default_llm_threshold_is_75():
    assert DEFAULT_LLM_THRESHOLD == 75


# --------------------------------------------------------------------
# Empty inputs
# --------------------------------------------------------------------


def test_match_questions_empty_fields_short_circuits():
    """``fields=[]`` → ``[]`` and zero LLM calls fire."""
    llm = AsyncMock()
    out = _run(match_questions(bank=[], fields=[], llm_client=llm))
    assert out == []
    llm.run_json_prompt.assert_not_awaited()


def test_match_questions_all_fields_skipped_returns_empty():
    """Every field is a skipped type → ``[]``."""
    fields = [
        {"label": "Upload resume", "field_type": "file"},
        {"label": "Submit", "field_type": "submit"},
    ]
    out = _run(match_questions(bank=[], fields=fields))
    assert out == []


# --------------------------------------------------------------------
# Pass 1 — rapidfuzz only
# --------------------------------------------------------------------


def test_match_questions_rapidfuzz_wins_on_clear_match():
    """A clear pattern match → ``source="rapidfuzz"`` and confidence high."""
    bank = [
        QABankRecord(
            id="q-years",
            question_pattern="years experience",
            canonical_question="How many years of experience do you have?",
            answer="5+ years of ML engineering.",
        ),
        QABankRecord(
            id="q-reloc",
            question_pattern="relocation",
            canonical_question="Are you willing to relocate?",
            answer="Yes.",
        ),
    ]
    fields = [
        FormFieldRecord(
            label="How many years of relevant work experience do you have?",
            field_type="textarea",
            field_id="f1",
        ),
    ]
    out = _run(match_questions(bank=bank, fields=fields))
    assert len(out) == 1
    assert out[0].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[0].entry_id == "q-years"
    assert out[0].confidence >= 0.85


def test_match_questions_multiple_pass1_matches_preserve_order():
    """Rapidfuzz matches are emitted in input order."""
    bank = [
        QABankRecord(
            id="q-why",
            question_pattern="why this company",
            canonical_question="Why do you want to work here?",
            answer="Because it's interesting.",
        ),
        QABankRecord(
            id="q-proud",
            question_pattern="proud project",
            canonical_question="What is your proudest project?",
            answer="Built a model serving platform.",
        ),
    ]
    # Realistic ATS-style long-form labels that clear the 85%
    # rapidfuzz threshold via token_set_ratio (the high-recall corner
    # of the three-flavour max).
    fields = [
        FormFieldRecord(
            label="Describe the engineering project you are most proud of.",
            field_type="textarea",
            field_id="f1",
        ),
        FormFieldRecord(
            label="Why are you interested in working at our company?",
            field_type="textarea",
            field_id="f2",
        ),
    ]
    out = _run(match_questions(bank=bank, fields=fields))
    assert len(out) == 2
    # Both fields match via rapidfuzz (proud set first, why set second;
    # order in the result list mirrors the input order).
    assert out[0].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[0].entry_id == "q-proud"
    assert out[1].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[1].entry_id == "q-why"


def test_match_questions_empty_bank_all_none():
    """Bank is empty → every field gets ``MATCH_SOURCE_NONE``."""
    fields = [
        FormFieldRecord(label="Years of experience?", field_type="text", field_id="f1"),
        FormFieldRecord(label="Willing to relocate?", field_type="radio", field_id="f2"),
    ]
    out = _run(match_questions(bank=[], fields=fields))
    assert len(out) == 2
    assert all(r.source == MATCH_SOURCE_NONE for r in out)
    assert all(r.entry_id is None for r in out)


# --------------------------------------------------------------------
# ``skip_no_answer_entries`` — blank answers filtered
# --------------------------------------------------------------------


def test_match_questions_blank_answers_filtered_from_pass_1():
    """``answer=None`` entries are skipped (so they can't accidentally match)."""
    bank = [
        # Blank entry — operator hasn't filled it yet.
        QABankRecord(
            id="q-blank",
            question_pattern="years experience",
            canonical_question="Years",
            answer=None,
        ),
        QABankRecord(
            id="q-filled",
            question_pattern="years experience",
            canonical_question="Years",
            answer="5+ years.",
        ),
    ]
    # Realistic long-form label that clears the 85% rapidfuzz
    # threshold via token_set_ratio / partial_ratio.
    fields = [
        FormFieldRecord(
            label="How many years of relevant work experience do you have?",
            field_id="f1",
        ),
    ]
    out = _run(match_questions(bank=bank, fields=fields))
    assert out[0].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[0].entry_id == "q-filled"


def test_match_questions_keep_blank_answers_when_disabled():
    """``skip_no_answer_entries=False`` keeps them so the operator can debug."""
    bank = [
        QABankRecord(
            id="q-blank",
            question_pattern="years experience",
            canonical_question="Years",
            answer=None,
        ),
        QABankRecord(
            id="q-filled",
            question_pattern="years experience",
            canonical_question="Years",
            answer="5+ years.",
        ),
    ]
    fields = [
        FormFieldRecord(
            label="How many years of professional experience do you have?",
            field_id="f1",
        ),
    ]
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            skip_no_answer_entries=False,
        )
    )
    # Both entries have the same question_pattern + canonical_question,
    # so rapidfuzz returns one of them (stable-sort tiebreak). Either
    # match is acceptable as long as both are rapidfuzz-sourced.
    assert out[0].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[0].entry_id in {"q-blank", "q-filled"}


# --------------------------------------------------------------------
# Pass 2 — LLM batch
# --------------------------------------------------------------------


def test_match_questions_llm_batch_picks_unmatched():
    """A weak label forces pass 2; LLM picks a sensible bank id."""
    bank = [
        QABankRecord(
            id="q-visa",
            question_pattern="work authorization",
            canonical_question="Are you authorized to work in the US?",
            answer="Yes, US citizen.",
        ),
        QABankRecord(
            id="q-why",
            question_pattern="why this company",
            canonical_question="Why this company?",
            answer="Because I want to.",
        ),
    ]
    # ``why us`` is short and unusual — rapidfuzz would NOT hit
    # ``why this company`` strongly (different word order without
    # token-sort catching commonalities). Forced low threshold.
    fields = [
        FormFieldRecord(label="Why us?", field_id="f1"),
    ]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = ('{"f1": "q-why"}', "meta/llama-3.1-70b-instruct")
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,  # force pass 2
        )
    )
    assert out[0].source == MATCH_SOURCE_LLM
    assert out[0].entry_id == "q-why"
    llm.run_json_prompt.assert_awaited_once()


def test_match_questions_llm_batch_returns_null_for_unknown():
    """LLM returns null → unmatched field collapses to MATCH_SOURCE_NONE."""
    bank = [
        QABankRecord(
            id="q-why",
            question_pattern="why this company",
            canonical_question="Why this company?",
            answer="Because.",
        ),
    ]
    fields = [
        FormFieldRecord(label="Why us?", field_id="f1"),
    ]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = ('{"f1": null}', "meta/llama-3.1-70b-instruct")
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,
        )
    )
    assert out[0].source == MATCH_SOURCE_NONE
    assert out[0].entry_id is None


def test_match_questions_llm_batch_one_await_for_all_unmatched():
    """Multiple unmatched fields → ONE ``run_json_prompt`` call (batched)."""
    bank = [
        QABankRecord(
            id="q-a",
            question_pattern="work authorization",
            canonical_question="Authorized?",
            answer="Yes.",
        ),
        QABankRecord(
            id="q-b",
            question_pattern="salary expectation",
            canonical_question="Comp?",
            answer="$150k+.",
        ),
    ]
    fields = [
        FormFieldRecord(label="Authorized?", field_id="f1"),
        FormFieldRecord(label="Comp?", field_id="f2"),
    ]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = (
        '{"f1": "q-a", "f2": "q-b"}',
        "meta/llama-3.1-70b-instruct",
    )
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,
        )
    )
    # Both rows mapped via LLM batch — exactly one await.
    llm.run_json_prompt.assert_awaited_once()
    assert out[0].source == MATCH_SOURCE_LLM
    assert out[0].entry_id == "q-a"
    assert out[1].source == MATCH_SOURCE_LLM
    assert out[1].entry_id == "q-b"


def test_match_questions_llm_error_collapses_unmatched_to_none():
    """LLM raises → unmatched fields all collapse to MATCH_SOURCE_NONE.

    A failure inside the LLM provider chain must NEVER abort the
    apply worker — instead the worker surfaces the unmatched
    fields so the operator can fill them in via the UI and the
    job is re-queued.
    """
    bank = [
        QABankRecord(
            id="q-a",
            question_pattern="work authorization",
            canonical_question="Authorized?",
            answer="Yes.",
        ),
    ]
    fields = [
        FormFieldRecord(label="Authorized?", field_id="f1"),
    ]
    llm = AsyncMock()
    llm.run_json_prompt.side_effect = RuntimeError("upstream 503")
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,
        )
    )
    assert out[0].source == MATCH_SOURCE_NONE
    assert out[0].entry_id is None


def test_match_questions_llm_hallucinated_id_collapses_field_to_none():
    """LLM returns a bank id that doesn't exist → that field collapses to None."""
    bank = [
        QABankRecord(
            id="q-a",
            question_pattern="auth",
            canonical_question="Authorized?",
            answer="Yes.",
        ),
    ]
    fields = [
        FormFieldRecord(label="Authorized?", field_id="f1"),
    ]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = (
        '{"f1": "q-XX-XX"}',
        "meta/llama-3.1-70b-instruct",
    )
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,
        )
    )
    assert out[0].source == MATCH_SOURCE_NONE
    assert out[0].entry_id is None


def test_match_questions_llm_malformed_response_parses_quietly():
    """LLM returns gibberish → every unmatched field collapses to None quietly."""
    bank = [
        QABankRecord(
            id="q-a",
            question_pattern="auth",
            canonical_question="Authorized?",
            answer="Yes.",
        ),
    ]
    fields = [FormFieldRecord(label="Authorized?", field_id="f1")]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = ("not json at all", "fake")
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,
        )
    )
    assert out[0].source == MATCH_SOURCE_NONE


def test_match_questions_llm_markdown_fence_stripped():
    """LLM wraps JSON in ``\\`\\`\\`json ... \\`\\`\\``` → still parsing."""
    bank = [
        QABankRecord(
            id="q-a",
            question_pattern="auth",
            canonical_question="Authorized?",
            answer="Yes.",
        ),
    ]
    fields = [FormFieldRecord(label="Authorized?", field_id="f1")]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = ('```json\n{"f1": "q-a"}\n```', "fake")
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
            rapidfuzz_threshold=99,
        )
    )
    assert out[0].source == MATCH_SOURCE_LLM
    assert out[0].entry_id == "q-a"


def test_match_questions_no_llm_client_rapidfuzz_unmatched_goes_to_none():
    """``llm_client=None`` + unmatched fields → every one collapses to None.

    A live worker with no LLM configured still surfaces the
    unmatched fields so the operator knows to fill them — no
    silent abort.
    """
    bank = [
        QABankRecord(
            id="q-a",
            question_pattern="auth",
            canonical_question="Authorized?",
            answer="Yes.",
        ),
    ]
    fields = [
        FormFieldRecord(label="Authorized?", field_id="f1"),
        FormFieldRecord(label="Years experience?", field_id="f2"),
    ]
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=None,
            rapidfuzz_threshold=99,
        )
    )
    assert all(r.source == MATCH_SOURCE_NONE for r in out)


def test_match_questions_mixed_pass1_and_pass2_in_input_order():
    """Some fields match by rapidfuzz; others fall through to LLM; order preserved.

    Three input fields: f1 ("proudest engineering project") matches
    rapidfuzz via token_sort_ratio + token_set_ratio; f2 ("Why us?")
    is unusual enough that rapidfuzz skips it → LLM batch picks
    q-why; f3 ("Years?") matches rapidfuzz via partial_ratio. Input
    order is f1, f2, f3 so the result list must preserve that order
    even though the LLM batch handled f2.
    """
    bank = [
        QABankRecord(
            id="q-why",
            question_pattern="why this company",
            canonical_question="Why this company?",
            answer="Because.",
        ),
        QABankRecord(
            id="q-proud",
            question_pattern="proud project",
            canonical_question="Proudest project?",
            answer="Built a model serving platform.",
        ),
        QABankRecord(
            id="q-years",
            question_pattern="years experience",
            canonical_question="Years?",
            answer="5+ years.",
        ),
    ]
    # Realistic long-form labels:
    #   f1 — token_set / partial_ratio catches "proudest engineering
    #        project" vs "proud project" (rapidfuzz wins).
    #   f2 — odd phrasing that no rapidfuzz flavor catches strongly
    #        → LLM batch picks q-why.
    #   f3 — partial_ratio catches "years of experience" vs
    #        "years experience" (rapidfuzz wins).
    fields = [
        FormFieldRecord(
            label="Tell us about your proudest engineering project and your role on it.",
            field_type="textarea",
            field_id="f1",
        ),
        FormFieldRecord(label="Why us?", field_id="f2"),
        FormFieldRecord(
            label="How many years of professional experience do you have?",
            field_id="f3",
        ),
    ]
    llm = AsyncMock()
    llm.run_json_prompt.return_value = (
        '{"f2": "q-why"}',
        "meta/llama-3.1-70b-instruct",
    )
    out = _run(
        match_questions(
            bank=bank,
            fields=fields,
            llm_client=llm,
        )
    )
    assert len(out) == 3
    # f1 — rapidfuzz wins via token_set on "proudest" + "proud".
    assert out[0].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[0].entry_id == "q-proud"
    # f2 — LLM batch handled the unusual label.
    assert out[1].source == MATCH_SOURCE_LLM
    assert out[1].entry_id == "q-why"
    # f3 — rapidfuzz wins via partial_ratio on "years experience".
    assert out[2].source == MATCH_SOURCE_RAPIDFUZZ
    assert out[2].entry_id == "q-years"
    # The LLM batch only ran once even though many things matched.
    llm.run_json_prompt.assert_awaited_once()
