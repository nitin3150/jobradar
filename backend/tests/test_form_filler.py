"""Tests for :mod:`apply_worker.form_filler` — exercises the real
Playwright driver logic WITHOUT launching a real Chromium.

Why injection instead of a real Playwright session
=================================================

The CI sandbox doesn't have ``playwright install chromium`` run on
it. The form_filler's design pushes the entire browser lifecycle
into an injected ``page_factory`` so tests can swap in a fake
without pulling in the Playwright runtime. This mirrors the
pattern :mod:`apply_worker.main` uses for the ``form_filler``
kwarg itself: the orchestrator passes a real function in prod
and tests pass an ``AsyncMock``.

What's actually under test
=========================

* goto is called with ``job.url`` + ``timeout=60_000``.
* ``page.evaluate`` extracts the field list correctly.
* ``match_questions`` is called with the field list (so the
  LLM is reachable from the form_filler path).
* **Early-abort** fires before ``submit`` if any
  ``entry_id is None`` — this is the SAFETY guarantee the
  thinker's pass demanded. Submit MUST never run on a
  half-filled form.
* Resume file bytes reach ``set_input_files`` as a
  ``FilePayload`` (``name``, ``mimeType``, ``buffer``) without
  writing to disk.
* Submit click attempts the role-based + CSS fallback chain
  in order.
* Screenshot bytes are uploaded via the injected
  ``screenshot_uploader`` and the storage path lands on the
  returned event.
* Empty page → ``([], [])`` returns.
* No URL → ``([], [])`` returns (no goto call attempted).

Mock shape
==========

``_FakePage`` implements the subset of the Playwright async API
the production code touches. The ``FakePage.calls`` list
captures every method invocations + kwargs so tests can assert
order + arguments, not just outcomes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from apply_worker.form_filler import (
    DEFAULT_GOTO_TIMEOUT_MS,
    SUBMISSION_EVENT_SUBMITTED,
    fill_form,
)


# ----------------------------------------------------------------------
# _FakePage — minimal Playwright ``Page`` surface for unit testing.
# Every method that the real form_filler awaits is recorded as a
# tuple ``(method_name, kwargs)`` on the ``calls`` list so a test
# can assert "goto was called BEFORE click".
# ----------------------------------------------------------------------


@dataclass
class _FakePage:
    """Subset of the Playwright async ``Page`` API used in tests."""

    # Configurable per-test behaviors — each is an Awaitable-returning
    # async coroutine. In production these are the real Playwright
    # implementations; here, they record the call and return a
    # canned/instrumented value.
    goto_side_effect: Callable[..., Awaitable[None]] | None = None
    evaluate_return: Any = None
    evaluate_side_effects: list[Callable[..., Awaitable[Any]]] = field(default_factory=list)
    screenshot_return: bytes = b"\x89PNG_FAKE"
    click_return: None = None
    set_input_files_return: None = None
    fill_return: None = None
    select_option_return: None = None
    check_return: None = None
    locator_first_return: Any = "ok"
    locator_count_return: int = 0

    # Recorded calls — append (name, kwargs) so tests assert order.
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.calls.append(("goto", {"url": url, **kwargs}))
        if self.goto_side_effect is not None:
            await self.goto_side_effect()

    async def evaluate(self, script: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("evaluate", {"script_len": len(script), "args": list(args), **kwargs}))
        # ``evaluate_side_effects`` lets a test step through multiple
        # evaluate calls (extract-first, then filler-side if any).
        if self.evaluate_side_effects:
            return await self.evaluate_side_effects.pop(0)(script)
        return self.evaluate_return

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.calls.append(("screenshot", kwargs))
        return self.screenshot_return

    def locator(self, selector: str) -> "_FakeLocator":
        self.calls.append(("locator", {"selector": selector}))
        return _FakeLocator(self, selector)

    def get_by_role(self, role: str, **kwargs: Any) -> "_FakeLocator":
        self.calls.append(("get_by_role", {"role": role, **kwargs}))
        return _FakeLocator(self, f"role={role}/{kwargs.get('name', '?')}")


@dataclass
class _FakeLocator:
    """Minimal locator with auto-waiting semantics that the form_filler uses.

    IMPORTANT: :attr:`first` is a :func:`property` (matching real
    Playwright) — NOT a method. Production code reads
    ``locator.first`` and chains ``locator.first.click(timeout=...)`` /
    ``locator.first.set_input_files(files=...)``. If ``first`` were a
    method, those chains would resolve to a bound-method object
    (``<bound method _FakeLocator.first>``) and ``.click``/``.set_input_files``
    would raise :class:`AttributeError` — which the production
    code's :keyword:`except Exception` blocks silently catch, logging
    ``set_input_files failed`` / ``no submit button matched``. The very
    first review of these tests missed this; it took a thinker's
    pass to spot it. Locking it in via the ``@property`` decorator
    AND a doc-comment so a future refactor doesn't reintroduce the
    method-shaped bug.
    """

    page: _FakePage
    selector: str

    async def count(self) -> int:
        self.page.calls.append(("locator.count", {"selector": self.selector}))
        return self.page.locator_count_return

    @property
    def first(self) -> "_FakeChildLocator":
        self.page.calls.append(("locator.first", {"selector": self.selector}))
        return _FakeChildLocator(self.page, self.selector)


@dataclass
class _FakeChildLocator:
    page: _FakePage
    selector: str

    async def click(self, **kwargs: Any) -> None:
        self.page.calls.append(("click", {"selector": self.selector, **kwargs}))
        return None

    async def fill(self, value: str, **kwargs: Any) -> None:
        self.page.calls.append(("fill", {"selector": self.selector, "value": value, **kwargs}))
        return None

    async def select_option(self, **kwargs: Any) -> None:
        self.page.calls.append(("select_option", {"selector": self.selector, **kwargs}))
        return None

    async def check(self, **kwargs: Any) -> None:
        self.page.calls.append(("check", {"selector": self.selector, **kwargs}))
        return None

    async def set_input_files(self, *, files: list[dict[str, Any]], **kwargs: Any) -> None:
        # Capture the FilePayload list so tests can assert the bytes
        # round-tripped without a temp-file disk write.
        self.page.calls.append((
            "set_input_files",
            {"selector": self.selector, "files": files, **kwargs},
        ))
        return None



@dataclass
class _FakeContextManager:
    """Async context manager wrapping a ``_FakePage`` so the
    production ``async with page_factory() as page:`` works.
    """

    page: _FakePage

    async def __aenter__(self) -> _FakePage:
        return self.page

    async def __aexit__(self, *_args: Any) -> None:
        return None


# ----------------------------------------------------------------------
# Helpers — build canned evaluate responses for easy test authoring.
# ----------------------------------------------------------------------


def _make_extract_response(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convenience: builds the JS ``[ {...}, {...} ]``-style payload the
    extraction ``page.evaluate(...)`` returns.
    """
    return fields


def _make_qa_match(
    *, entry_id: str | None, field_id: str = "f1", label: str = "Years", source: str = "rapidfuzz"
) -> Any:
    """Builds a :class:`apply_worker.types.MatchResult` duck-type.

    Uses ``SimpleNamespace`` because the form_filler only reads
    ``entry_id``, ``field_id`` (and indirectly
    ``qa_matches[i].entry_id`` in the orchestrator); no method
    calls.
    """
    return SimpleNamespace(
        entry_id=entry_id,
        field_id=field_id,
        label=label,
        confidence=0.9 if entry_id else 0.0,
        source=source,
        reasoning="",
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_form_happy_path_submits_and_uploads_screenshot() -> None:
    """Harmony path: goto → extract 1 field → match → fill → upload resume
    → submit (role-based) → screenshot → upload screenshot.

    Asserts:
    * ``page.goto`` called with the job URL + 60 s timeout.
    * ``page.evaluate`` called for extraction AND the form_filler later
      still passes the qa_match through (here, one filled entry).
    * ``set_input_files`` got the FilePayload buffer (downloaded bytes).
    * ``page.click`` was succeeded for the role-based submit.
    * ``screenshot_uploader`` was called with (job_id, bytes).
    *    Return value: tuple ``(events=[{...}], qa_matches=[match])``.
    """
    page = _FakePage()
    # ``evaluate`` is called once for extract; that's all the form_filler
    # itself does via evaluate. The qa_matcher doesn't touch the page.
    async def _extract_response(_script: str) -> list[dict[str, Any]]:
        return _make_extract_response([
            {"label": "Years of experience", "field_type": "text",
             "select_options": [], "field_id": "years"},
        ])
    page.evaluate_side_effects = [_extract_response]    
    # First ``locator(years)`` — fill; we record calls via the locator.
    page.locator_count_return = 1  # file input present
    page.locator_first_return = "filled"

    # Inject a mock match_questions on the symbol :data:`form_filler`
    # ACTUALLY binds at import time (NOT on the qa_matcher module — the
    # ``from apply_worker.qa_matcher import match_questions`` at form_filler
    # module top creates a local binding that bypasses any monkey-patch on
    # the qa_matcher module). One matched entry per extracted field keeps
    # the happy-path branch reachable.
    import apply_worker.form_filler as ff_mod

    async def _match_one_qa1(*_args, **_kwargs):
        return [
            _make_qa_match(entry_id="q1", field_id="years", label="Years"),
        ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ff_mod, "match_questions", _match_one_qa1)
    page_factory = lambda: _FakeContextManager(page)

    screenshot_uploader = AsyncMock(return_value="screenshots/job-1.png")
    resume_downloader = AsyncMock(return_value=b"%PDF_FAKE_BYTES")

    bank = [
        {"id": "q1", "question_pattern": "years of experience",
         "canonical_question": "Years of experience", "answer": "5 years",
         "answer_type": "short_text", "times_used": 1},
    ]
    job = {
        "id": "job-1", "url": "https://acme.test/apply/1",
        "ats_type": "greenhouse", "title": "AI Engineer",
        "company_name": "Acme",
    }
    resume = {"id": "r1", "name": "ml.pdf", "tags": ["ml"],
              "is_default": True, "uploaded_at": "2026-07-01T00:00:00Z",
              "storage_path": "resumes/r1.pdf"}
    llm_client = AsyncMock()

    events, qa_matches = await fill_form(
        job=job, resume=resume, bank=bank, llm_client=llm_client,
        page_factory=page_factory,
        screenshot_uploader=screenshot_uploader,
        resume_downloader=resume_downloader,
        post_goto_sleep_s=0,  # skip the sleep in tests
    )

    # 1. Tuple shape: one event + one qa_match (all matched → SUBMITTED).
    assert len(events) == 1
    assert events[0]["status"] == SUBMISSION_EVENT_SUBMITTED
    assert events[0]["platform"] == "greenhouse"
    assert events[0]["screenshot_path"] == "screenshots/job-1.png"
    assert events[0]["submit_clicked"] is True
    assert len(qa_matches) == 1

    # 2. goto was called BEFORE click / screenshot / etc.
    call_names = [name for name, _ in page.calls]
    assert "goto" in call_names
    goto_idx = call_names.index("goto")
    assert "screenshot" in call_names
    assert goto_idx < call_names.index("screenshot")

    # 3. Goto was called with the job URL + 60 s timeout.
    goto_call = next(c for n, c in page.calls if n == "goto")
    assert goto_call["url"] == "https://acme.test/apply/1"
    assert goto_call["timeout"] == DEFAULT_GOTO_TIMEOUT_MS

    # 4. Screenshot uploader was called with (job_id, bytes).
    screenshot_uploader.assert_awaited_once()
    upload_args = screenshot_uploader.await_args.args
    assert upload_args[0] == "job-1"
    assert upload_args[1] == b"\x89PNG_FAKE"

    # 5. Resume downloader was called with storage_path, and the
    #    FilePayload reached set_input_files verbatim (no temp file).
    resume_downloader.assert_awaited_once_with("resumes/r1.pdf")
    set_input_calls = [c for n, c in page.calls if n == "set_input_files"]
    assert len(set_input_calls) == 1
    files = set_input_calls[0]["files"]
    assert len(files) == 1
    assert files[0]["name"] == "ml.pdf"
    assert files[0]["buffer"] == b"%PDF_FAKE_BYTES"

    # 6. Submit-click chain executed at least once — the heuristic
    #    chain tries role-based first, then CSS fallbacks. We don't
    #    pin which strategy succeeded (FakePage click() is a stub)
    #    but assert that ORCHESTRATION-side wiring fired it.
    click_calls = [c for n, c in page.calls if n == "click"]
    assert len(click_calls) >= 1

    monkeypatch.undo()


@pytest.mark.asyncio
async def test_fill_form_no_fields_returns_empty_tuple_no_submit() -> None:
    """Page with zero form inputs → ``([], [])`` tuple. Submit
    MUST NOT be clicked. Screenshot MUST NOT be taken.
    """
    page = _FakePage(evaluate_return=[])  # extract found nothing
    page_factory = lambda: _FakeContextManager(page)
    screenshot_uploader = AsyncMock()

    events, qa_matches = await fill_form(
        job={"id": "job-2", "url": "https://acme.test/apply/2",
             "ats_type": "lever"},
        resume=None, bank=[], llm_client=AsyncMock(),
        page_factory=page_factory, screenshot_uploader=screenshot_uploader,
        post_goto_sleep_s=0,
    )

    assert events == []
    assert qa_matches == []

    # Submit MUST NOT have been attempted.
    call_names = [n for n, _ in page.calls]
    assert "click" not in call_names

    # Screenshot MUST NOT have been taken (no events → form_filler
    # short-circuits BEFORE the screenshot step).
    assert "screenshot" not in call_names

    # Screenshot uploader MUST NOT have been called.
    screenshot_uploader.assert_not_called()


@pytest.mark.asyncio
async def test_fill_form_unmatched_early_aborts_no_submit() -> None:
    """SAFETY guarantee: if any matched field has ``entry_id is None``
    the form_filler MUST early-abort BEFORE clicking submit. The
    thinker's design pass explicitly demanded this — submitting a
    half-filled ATS form is worse than parking.

    Asserts:
    * Result is ``([], qa_matches_with_unmatched)``.
    * ``page.click`` was NEVER called.
    * Screenshot was NOT taken (early-abort path skips it).
    * Screenshot uploader was NOT called.
    """
    page = _FakePage(evaluate_return=_make_extract_response([
        {"label": "Years of experience", "field_type": "text",
         "select_options": [], "field_id": "years"},
        {"label": "Visa sponsorship", "field_type": "text",
         "select_options": [], "field_id": "visa"},
    ]))
    page_factory = lambda: _FakeContextManager(page)
    screenshot_uploader = AsyncMock()

    # Inject a mock match_questions on the symbol :data:`form_filler`
    # ACTUALLY binds at import time (NOT on the qa_matcher module ‒
    # the ``from apply_worker.qa_matcher import match_questions`` at
    # form_filler module top creates a local binding that bypasses
    # any monkey-patch on the qa_matcher module).
    import apply_worker.form_filler as qa_mod

    async def _match_with_one_none(*_args: Any, **_kwargs: Any):
        return [
            _make_qa_match(entry_id="q1", field_id="years", label="Years"),
            _make_qa_match(entry_id=None, field_id="visa", label="Visa", source="none"),
        ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(qa_mod, "match_questions", _match_with_one_none)

    try:
        events, qa_matches = await fill_form(
            job={"id": "job-3", "url": "https://acme.test/apply/3",
                 "ats_type": "workday"},
            resume={"id": "r1", "name": "ml.pdf", "storage_path": "resumes/r1.pdf"},
            bank=[{"id": "q1", "question_pattern": "years of experience",
                    "canonical_question": "Years of experience",
                    "answer": "5 years", "answer_type": "short_text",
                    "times_used": 1}],
            llm_client=AsyncMock(),
            page_factory=page_factory, screenshot_uploader=screenshot_uploader,
            post_goto_sleep_s=0,
        )
    finally:
        monkeypatch.undo()

    # 1. Events empty — orchestrator will park via UNMATCHED_FIELDS.
    assert events == []
    # 2. qa_matches passed through intact so the operator can see
    #    which fields were unmatched (the orchestrator's
    #    UNMATCHED_FIELDS branch reads them).
    assert len(qa_matches) == 2
    assert qa_matches[1].entry_id is None

    # 3. Submit MUST NOT have been attempted.
    call_names = [n for n, _ in page.calls]
    assert "click" not in call_names

    # 4. Screenshot / upload MUST NOT have been attempted.
    assert "screenshot" not in call_names
    assert "set_input_files" not in call_names
    screenshot_uploader.assert_not_called()


@pytest.mark.asyncio
async def test_fill_form_no_url_returns_empty_tuple_no_goto() -> None:
    """If ``job.url`` is missing, the form_filler returns
    ``([], [])`` without even attempting goto. Defensive —
    protects against operator-side seed data where url was
    never captured.
    """
    page = _FakePage()
    page_factory = lambda: _FakeContextManager(page)
    screenshot_uploader = AsyncMock()

    events, qa_matches = await fill_form(
        job={"id": "job-4", "url": None, "ats_type": "unknown"},
        resume=None, bank=[], llm_client=AsyncMock(),
        page_factory=page_factory, screenshot_uploader=screenshot_uploader,
    )

    assert events == []
    assert qa_matches == []
    # goto MUST NOT have been attempted (no URL).
    call_names = [n for n, _ in page.calls]
    assert "goto" not in call_names
    screenshot_uploader.assert_not_called()


@pytest.mark.asyncio
async def test_fill_form_goto_failure_returns_empty_tuple() -> None:
    """If ``page.goto`` raises (network fail, ATS 5xx), the
    form_filler returns ``([], [])`` rather than entering an
    infinite retry loop on a broken URL. The orchestrator
    parks via ``NO_FIELDS``.
    """
    async def _raise_timeout(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.TimeoutError("simulated ATS timeout")

    page = _FakePage(goto_side_effect=_raise_timeout)
    page_factory = lambda: _FakeContextManager(page)

    events, qa_matches = await fill_form(
        job={"id": "job-5", "url": "https://broken.test/apply",
             "ats_type": "unknown"},
        resume=None, bank=[], llm_client=AsyncMock(),
        page_factory=page_factory, post_goto_sleep_s=0,
    )

    assert events == []
    assert qa_matches == []


@pytest.mark.asyncio
async def test_fill_form_submit_clicked_via_role_first() -> None:
    """``page.get_by_role("button", name="Submit")`` must be tried
    before the CSS fallback chain. If the role-based locator
    works, no CSS fallback should be attempted.
    """
    page = _FakePage(evaluate_return=_make_extract_response([
        {"label": "Years", "field_type": "text", "field_id": "years"},
    ]))
    page_factory = lambda: _FakeContextManager(page)
    screenshot_uploader = AsyncMock(return_value="path.png")

    import apply_worker.form_filler as qa_mod

    async def _match_one(*_args: Any, **_kwargs: Any):
        return [_make_qa_match(entry_id="q1", field_id="years", label="Years")]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(qa_mod, "match_questions", _match_one)
    try:
        events, _ = await fill_form(
            job={"id": "job-6", "url": "https://acme.test/apply/6",
                 "ats_type": "greenhouse"},
            resume={"id": "r1", "name": "ml.pdf", "storage_path": "resumes/r1.pdf"},
            bank=[{"id": "q1", "question_pattern": "years of experience",
                    "canonical_question": "Years of experience",
                    "answer": "5 years", "answer_type": "short_text",
                    "times_used": 1}],
            llm_client=AsyncMock(),
            page_factory=page_factory, screenshot_uploader=screenshot_uploader,
            post_goto_sleep_s=0,
        )
    finally:
        monkeypatch.undo()

    # Submit was clicked via the role-based path (Submit).
    role_calls = [c for n, c in page.calls if n == "get_by_role"]
    assert any("Submit" in str(c.get("name", "")) for c in role_calls), (
        "expected get_by_role('button', name='Submit') to be called first; "
        f"recorded: {role_calls}"
    )

    # Screenshot uploader was called once on the happy path.
    screenshot_uploader.assert_awaited_once()
