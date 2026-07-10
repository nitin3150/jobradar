"""Auto-apply worker — pure-Python algorithmic pieces only.

The Playwright-driven form_filler ships in a followup; this package
holds the deterministic scanner/decision logic so the worker can be
tested + fast-iterated without a full browser stack:

* :mod:`apply_worker.resume_picker` — tag-match → LLM-fallback resume selector.
* :mod:`apply_worker.qa_matcher` — two-pass (rapidfuzz → LLM) Q&A bank matcher.

Both modules take plain ``dict`` inputs (stubs of ``Job`` /
``Resume`` / ``QABankEntry`` / ``FormField``). Tests construct the
stubs directly — no DB, no Playwright, no real LLM required to
exercise the happy paths + edge cases. The orchestration layer
that wires them to actual database reads lives in (later)
``apply_worker/main.py``.

Module design contract
======================

* Async-first — every public coroutine ``await``s at most once for
  the LLM fallback path. Local-only paths are sync-friendly so
  tests can call them via ``asyncio.run(matcher(local_only=True))``.
* Provider abstraction — the LLM client is an optional
  ``llm_client`` kwarg so unit tests can pass an
  ``unittest.mock.AsyncMock`` that mimics the same shape as
  :class:`services.llm_client.LLMClient`. Production passes an
  :class:`LLMClient` constructed via ``LLMClient.from_env()``.
* No DB I/O — DB reads happen in caller-side glue. Modules stay
  unit-testable without a Postgres connection.
"""
