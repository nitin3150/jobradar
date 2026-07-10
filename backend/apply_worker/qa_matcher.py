"""apply_worker.qa_matcher — two-pass (rapidfuzz → LLM) Q&A bank matcher.

Pure Python, deterministic when given good inputs. The apply worker
calls :func:`match_questions` once per ``Application`` form after
the Playwright form_filler extracts the field list.

Algorithm
=========

1. **Pass 1 — rapidfuzz local match.** For each form field, take
   the MAX of three ``rapidfuzz.fuzz`` flavors against each bank
   entry's ``question_pattern``:

   * ``partial_ratio`` — substring match, rewards short pattern +
     long label.
   * ``token_sort_ratio`` — sorts both strings alphabetically, then
     compares. Catches label-vs-pattern word-order variations
     (e.g. ``"what is your earliest start date"`` vs
     ``"start date"``).
   * ``token_set_ratio`` — set-based word overlap. Tolerates extra
     words in the label that aren't in the pattern.

   If the max score across all bank entries is
   ``>= rapidfuzz_threshold`` (default 85), that bank entry wins
   with ``source="rapidfuzz"``. Zero LLM calls fire for clear-cut
   matches.

2. **Pass 2 — LLM batch semantic.** For each field that didn't
   pass rapidfuzz, build a single LLM prompt with ALL of them
   plus each field's top-3 candidates by rapidfuzz score. The
   response is a JSON dict ``field_id -> best_bank_id_or_null``.
   Confidence threshold (``llm_threshold`` default 75) gates the
   final accept.

   Batching cuts the call count from ``N_unmatched * 1`` to
   ``1`` per job. At 5 unmatched fields per form, that's a 5×
   reduction in LLM RPS cost — meaningful on a 40-RPM free-tier
   NVIDIA key.

3. **No-match flag.** Any field with neither a rapidfuzz hit
   NOR an LLM-confident pick is flagged in the result with
   ``entry_id=None, source="none"``. The worker surfaces these
   flags up the call chain; if ANY field is unmatched, the apply
   step inserts a fresh QABankEntry with ``answer=None`` and
   aborts (per the user's spec — "abort + flag on unknown field").

Notes on the LLM plumbing
=========================

The LLM batch call goes through
:meth:`services.llm_client.LLMClient.run_json_prompt` so we
inherit the standard retry-then-fallback contract (NVIDIA → Groq,
1 retry per provider on transient, advance on permanent) WITHOUT
reaching into private LLMClient internals (``self._clients``,
``self.providers``). When that method itself raises
:class:`RuntimeError` nobody is left to retry, we collapse every
unmatched field to ``(None, "none")`` and the apply worker's
abort+flag surface takes over.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from rapidfuzz import fuzz

from apply_worker.types import (
    FormFieldRecord,
    MATCH_SOURCE_LLM,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_RAPIDFUZZ,
    MatchResult,
    QABankRecord,
)
from services.llm_client import LLMClient

_logger = logging.getLogger("jobradar.apply_worker")


# Env-var defaults — operators can tune from .env without code change.
DEFAULT_RAPIDFUZZ_THRESHOLD = int(
    os.environ.get("QA_RAPIDFUZZ_THRESHOLD", "85")
)
DEFAULT_LLM_THRESHOLD = int(os.environ.get("QA_LLM_THRESHOLD", "75"))


# System prompt for the LLM semantic pass. Tight, deterministic,
# and bounded in token cost — the LLM has to emit a strict JSON
# object, the user prompt is bounded by ``max(3, len(fields))``
# candidates per field, and ``run_json_prompt`` is called with
# ``max_tokens=600``. JSON-or-bust so :func:`_parse_llm_response`
# can trust the format.
_LLM_MATCH_SYSTEM_PROMPT: str = (
    "You are a job-application Q&A matcher. Given a list of form "
    "field labels (with their associated candidate Q&A bank "
    "answers), return a strict JSON object mapping each "
    "field_id to the single best bank answer id, or null if no "
    "candidate safely answers that field. Be conservative — "
    "returning null is PREFERRED over a forced match that doesn't "
    "actually fit, because the apply worker will abort on null "
    "and surface the field for the operator to fill via the UI.\n\n"
    "Match logic: pick the candidate whose ``question_pattern`` is "
    "semantically equivalent to the field label. Reject candidates "
    "that are CLOSE but not equivalent (e.g. 'years experience' "
    "should NOT match 'notice period' even though both are about "
    "timing). When in doubt, return null.\n\n"
    "Output shape (strict; no preamble, no markdown):\n"
    '{"f1": "<bank_id_or_null>", "f2": "<bank_id_or_null>", ...}\n\n'
    "Output every field_id exactly once, in any order."
)


# -----------------------------------------------------------------
# Field types that don't need Q&A matching — they get filled
# differently by the future form_filler (file picker for ``file``,
# boolean toggle for ``checkbox``/``radio``, enum selection for
# ``select``). Deliberately a frozenset so lookups stay O(1).
# -----------------------------------------------------------------
_SKIPPED_FIELD_TYPES = frozenset({"file", "submit", "button", "hidden"})


def _label_to_score(label: str, pattern: str) -> float:
    """Best of three ``rapidfuzz`` flavors, normalised to 0.0-1.0.

    Each flavor catches a different phrasing variation:

    * ``partial_ratio`` — substring partial match; rewards short
      pattern + long label (e.g. label="how many years of
      experience do you have?", pattern="years experience").
      Without this, a long label with a short pattern substring
      would score low in plain ``ratio``.

    * ``token_sort_ratio`` — word-order-insensitive exact match
      after sorting both strings' tokens. Catches ``"start date
      earliest"`` vs ``"earliest start date"``.

    * ``token_set_ratio`` — set-based word overlap. Tolerates
      label having extra words the pattern doesn't contain
      (e.g. label mentions relocation AND remote, pattern is
      just "relocation").

    Picking the MAX means a single label can score high if any
    one of these dimensions matches — the union of three is the
    recall we want. Token-based scores are 0-100 in rapidfuzz;
    we normalise to 0.0-1.0 to match the LLM confidence scale.
    """
    if not label or not pattern:
        return 0.0
    return max(
        fuzz.partial_ratio(label, pattern),
        fuzz.token_sort_ratio(label, pattern),
        fuzz.token_set_ratio(label, pattern),
    ) / 100.0


def _per_field_top_k(
    field: FormFieldRecord,
    bank: list[QABankRecord],
    k: int = 3,
) -> list[tuple[QABankRecord, float]]:
    """Top-K rapidfuzz candidates for a single field.

    Future-proofing for the LLM prompt: when the bank has more than
    K entries, the model sees the field's K most-plausible
    candidates rather than the full bank. This keeps the prompt
    bounded when the operator has hundreds of bank rows.
    """
    scored: list[tuple[QABankRecord, float]] = []
    for entry in bank:
        scored.append((entry, _label_to_score(field.label, entry.question_pattern)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]


def _build_llm_user_prompt(
    unmatched_fields: list[FormFieldRecord],
    candidate_k_per_field: dict[str, list[tuple[QABankRecord, float]]],
    all_bank_ids: list[str],
) -> str:
    """Compact, token-bounded prompt for the semantic-pass LLM call.

    Strict JSON shape (parsed via :func:`json.loads` in
    :func:`_parse_llm_response`). The ``all_bank_ids`` allow-list
    keeps the LLM from hallucinating bank ids it didn't see — a
    defensive parse step still re-validates against the actual
    bank list.
    """
    fields_section = [
        {
            "field_id": f.field_id,
            "label": f.label,
            "field_type": f.field_type,
            "select_options": f.select_options,
        }
        for f in unmatched_fields
    ]
    candidates_section = {
        fid: [
            {
                "bank_id": entry.id,
                "question_pattern": entry.question_pattern,
                "canonical_question": entry.canonical_question,
                "answer_excerpt": (entry.answer or "")[:120],
            }
            for entry, _score in candidates
        ]
        for fid, candidates in candidate_k_per_field.items()
    }
    payload = {
        "fields": fields_section,
        "candidates_per_field": candidates_section,
        "all_bank_ids": all_bank_ids,
    }
    return json.dumps(payload, indent=2)


def _parse_llm_response(
    content: str,
    field_ids: list[str],
    valid_bank_ids: set[str],
) -> dict[str, str | None]:
    """Parse the LLM's strict-JSON response.

    Defensive on three axes:
    * Markdown code fences (``\\`\\`\\`json ... \\`\\`\\```)
      stripped, then ``json.loads``.
    * Whitespace wrapping (sliced to outermost ``{...}`` if
      direct fails).
    * Hallucinated ids (any bank id NOT in the allow-list is
      collapsed to ``None`` so the worker will fallback-flag).
    * Missing field ids (any input field_id missing from the
      response defaults to ``None``; the caller decides whether
      that's an abort signal).
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n ")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {fid: None for fid in field_ids}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {fid: None for fid in field_ids}
    if not isinstance(data, dict):
        return {fid: None for fid in field_ids}
    result: dict[str, str | None] = {}
    for fid in field_ids:
        raw = data.get(fid)
        if raw is None:
            result[fid] = None
        elif isinstance(raw, str) and raw in valid_bank_ids:
            result[fid] = raw
        else:
            # Hallucinated id, non-string, or unknown id — collapse
            # to None so the apply worker safely aborts on this
            # field rather than attempting to fill with junk.
            result[fid] = None
    return result


# -----------------------------------------------------------------
# Public API
# -----------------------------------------------------------------


async def match_questions(
    bank: list[dict[str, Any] | QABankRecord] | None,
    fields: list[dict[str, Any] | FormFieldRecord] | None,
    *,
    llm_client: LLMClient | None = None,
    rapidfuzz_threshold: int | None = None,
    llm_threshold: int | None = None,
    candidate_k: int = 3,
    skip_no_answer_entries: bool = True,
) -> list[MatchResult]:
    """Two-pass matcher. Returns one :class:`MatchResult` per field.

    The list is in the same order as ``fields`` — callers index by
    position, no need to re-key by ``field_id``. Empty ``fields``
    short-circuits to ``[]`` rather than firing the LLM on nothing.

    Args:
        bank: List of Q&A bank records or their plain-dict
            equivalent. ``None`` is treated as empty list. When a
            record's ``answer`` is ``None`` and
            ``skip_no_answer_entries=True`` (default), the entry is
            filtered out from BOTH passes — we can't fill from a
            blank entry, and matching-then-flagging-as-blank wastes
            a slot in the apply queue.
        fields: Form fields as extracted by the future Playwright
            form_filler. ``file``/``submit``/``button``/``hidden``
            types are silently filtered — they don't need Q&A
            matching (they're for file picker, button clicks).
        llm_client: Optional :class:`LLMClient`. Required for
            pass 2. When ``None``, unmatched fields all return
            ``(None, "none")`` — the apply worker will see ALL such
            fields as needing operator attention.
        rapidfuzz_threshold: 0-100. None → default
            (:data:`DEFAULT_RAPIDFUZZ_THRESHOLD` from env).
        llm_threshold: Same — pass-2 acceptance cut-off.
        candidate_k: Number of top rapidfuzz candidates per field
            to include in the LLM prompt.

    Returns:
        List of :class:`MatchResult` in input order. An empty
        result-list is returned for empty ``fields``.
    """
    if not fields:
        return []

    # ----- Normalise inputs ----------------------------------------------
    bank_records: list[QABankRecord] = []
    for entry in bank or []:
        rec = entry if isinstance(entry, QABankRecord) else QABankRecord.from_data(entry)
        if skip_no_answer_entries and rec.answer is None:
            continue
        bank_records.append(rec)

    parsed_fields: list[FormFieldRecord] = []
    for idx, fld in enumerate(fields):
        rec = (
            fld
            if isinstance(fld, FormFieldRecord)
            else FormFieldRecord.from_data(fld)
        )
        if rec.field_type in _SKIPPED_FIELD_TYPES:
            continue
        # Synthesise an id if the form_filler didn't provide one so
        # batched LLM responses still key unambiguously. Position-
        # based ('f1', 'f2', ...) is deterministic and avoids label-
        # string collisions when two fields share a label.
        if not rec.field_id:
            rec.field_id = f"f{idx + 1}"
        parsed_fields.append(rec)

    if not parsed_fields:
        return []

    rf_threshold = (
        float(rapidfuzz_threshold)
        if rapidfuzz_threshold is not None
        else float(DEFAULT_RAPIDFUZZ_THRESHOLD)
    ) / 100.0
    llm_threshold_pct = (
        int(llm_threshold)
        if llm_threshold is not None
        else int(DEFAULT_LLM_THRESHOLD)
    )

    # ----- Pass 1: rapidfuzz ---------------------------------------------
    # We track the original index so the final result list is in
    # ``fields``-input order regardless of which pass handled the row.
    results: list[MatchResult | None] = [None] * len(parsed_fields)
    unmatched_fields: list[FormFieldRecord] = []
    candidates_per_field: dict[str, list[tuple[QABankRecord, float]]] = {}

    for idx, field in enumerate(parsed_fields):
        if not bank_records:
            # Empty bank — every field is unmatched on pass 1
            # (still useful to surface each unlabeled field).
            results[idx] = MatchResult(
                label=field.label,
                field_id=field.field_id,
                entry_id=None,
                confidence=0.0,
                source=MATCH_SOURCE_NONE,
            )
            continue
        top_k = _per_field_top_k(field, bank_records, k=candidate_k)
        candidates_per_field[field.field_id] = top_k
        top_entry, top_score = top_k[0]
        if top_score >= rf_threshold:
            results[idx] = MatchResult(
                label=field.label,
                field_id=field.field_id,
                entry_id=top_entry.id,
                confidence=float(top_score),
                source=MATCH_SOURCE_RAPIDFUZZ,
            )
        else:
            unmatched_fields.append(field)

    # ----- Pass 2: LLM batch ---------------------------------------------
    if unmatched_fields and llm_client is not None and bank_records:
        bank_id_set = {b.id for b in bank_records}
        unmatched_ids = [f.field_id for f in unmatched_fields]
        user_prompt = _build_llm_user_prompt(
            unmatched_fields, candidates_per_field, sorted(bank_id_set)
        )
        try:
            # ``max_tokens=600`` because the response is bounded by
            # ``len(unmatched_fields)`` entries — at 5 unmatched
            # fields, the JSON is ~150 chars; the cap is defensive
            # against the model hallucinating extra fields or
            # surrounding prose.
            content, _model = await llm_client.run_json_prompt(
                system_prompt=_LLM_MATCH_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=600,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 — LLM fallback contract
            _logger.warning(
                "match_questions: LLM batch failed (%s); falling back "
                "to MATCH_SOURCE_NONE for all unmatched fields",
                type(exc).__name__,
            )
            content = "{}"

        llm_response = _parse_llm_response(
            content,
            field_ids=unmatched_ids,
            valid_bank_ids=bank_id_set,
        )

        # Map unmatched field_ids back to their original ``parsed_fields``
        # index so we update ``results`` in place while preserving order.
        field_id_to_idx = {f.field_id: i for i, f in enumerate(parsed_fields)}
        for fid, bank_id in llm_response.items():
            if bank_id is None:
                continue
            target_idx = field_id_to_idx.get(fid)
            if target_idx is None:
                continue
            entry = next((b for b in bank_records if b.id == bank_id), None)
            if entry is None:
                continue
            field = parsed_fields[target_idx]
            results[target_idx] = MatchResult(
                label=field.label,
                field_id=fid,
                entry_id=bank_id,
                confidence=float(llm_threshold_pct) / 100.0,
                source=MATCH_SOURCE_LLM,
                reasoning="llm semantic match (pass 2 batch)",
            )

    # ----- Fill remaining None slots with MATCH_SOURCE_NONE ---------------
    for idx, slot in enumerate(results):
        if slot is None:
            field = parsed_fields[idx]
            results[idx] = MatchResult(
                label=field.label,
                field_id=field.field_id,
                entry_id=None,
                confidence=0.0,
                source=MATCH_SOURCE_NONE,
            )
    return results


__all__ = [
    "DEFAULT_RAPIDFUZZ_THRESHOLD",
    "DEFAULT_LLM_THRESHOLD",
    "match_questions",
]
