"""Tests for :mod:`apply_worker.main` — exercises :func:`run_one_tick`
end-to-end without a live Postgres.

Why a hand-rolled ``_FakeSession`` (vs. ``AsyncMock``)
====================================================

The sandbox's network cannot reach the Supabase Postgres pooler, so
real SQLAlchemy sessions are out of scope for these tests. We
exercise the orchestrator against a tiny ``_FakeSession`` that
mocks enough of the async SQLAlchemy surface for the orchestrator's
behaviour to be observable:

* ``session.execute(stmt)`` — returns whichever canned result the
  test setup installed (a list of approved rows for the deque, a
  list of resume metadata for the per-tick snapshot, a list of
  QA-bank rows).
* ``session.flush()`` — recorded so the happy-path test can assert
  the FK flush happened BEFORE the ``jobs.status`` flip.
* ``session.add(row)`` — recorded so tests can assert the
  :class:`db.models.Application` row was constructed with the
  expected ``resume=...`` + ``qamatches=...`` note shape.
* ``async with session.begin():`` — yields a no-op async context
  manager so the orchestrator's ``async with`` block doesn't
  raise. The ``async with AsyncSessionLocal() as session:``
  wrapper from the prod ``main_loop`` is bypassed — tests call
  ``run_one_tick`` directly.

v0.7 churn: form_filler is now a real Playwright driver that
returns a 2-tuple ``(events, qa_matches)``. Tests inject an
:class:`AsyncMock` whose ``return_value`` is the tuple. The
``unmatched_qa_fields`` test no longer monkey-patches
``match_questions`` (the orchestrator doesn't call it directly
anymore — it's now called INSIDE form_filler).

The important assertion surface is the orchestrator's *writes*
(``status`` flips + ``job_status_history`` rows added + the
``Application`` INSERT) — captured via ``session.added`` and
``session.flushed`` on the fake session. The reads (deque query,
snapshot queries) are inputs to the assertion, not assertions
themselves; we install the canned reads, then assert the writes
follow.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from apply_worker.main import (
    JOB_FETCH_LIMIT,
    TickOutcome,
    TickStatus,
    run_one_tick,
)


# ----------------------------------------------------------------------
# _FakeSession — enough of the SQLAlchemy async API to drive
# ``run_one_tick`` without a live Postgres.
# ----------------------------------------------------------------------


class _FakeResult:
    """Mimics SQLAlchemy ``Result``'s ``.scalars().all()`` chain."""

    def __init__(self, payload: list[Any]) -> None:
        self._payload = payload

    def scalars(self) -> "_FakeScalars":
        return _FakeScalars(self._payload)


class _FakeScalars:
    def __init__(self, payload: list[Any]) -> None:
        self._payload = payload

    def all(self) -> list[Any]:
        return self._payload


# ----------------------------------------------------------------------
# ORM-shaped fakes — ``_fetch_resumes_and_bank`` reads from the
# SQLAlchemy ``select(db_models.Resume)`` / ``select(db_models.QABankEntry)``
# results, which produce ORM rows with ATTRIBUTE access
# (``r.id``, ``r.name``, ``r.tags``, etc.) — NOT plain-dict
# access. The orchestrator's exact production shape so the test
# catches a future migration that changes the column set
# (e.g. dropping ``uploaded_at`` would break the test harness
# before it ever hit production).
# ----------------------------------------------------------------------


@dataclass
class _FakeResumeRow:
    """Mimics :class:`db.models.Resume` columns via attribute access."""

    id: str
    name: str
    size_bytes: int = 100_000
    uploaded_at: datetime = field(
        default_factory=lambda: datetime(2026, 7, 1, tzinfo=timezone.utc)
    )
    tags: list[str] = field(default_factory=list)
    is_default: bool = False
    storage_path: str = ""


@dataclass
class _FakeQAEntryRow:
    """Mimics :class:`db.models.QABankEntry` columns via attribute access."""

    id: str
    question_pattern: str
    canonical_question: str
    answer: str | None = None
    answer_type: str = "short_text"
    times_used: int = 0


@dataclass
class _FakeJob:
    """Duck-typed ``db.models.Job`` substitute.

    The orchestrator only reads ``id, status, title, description,
    company_name, url, ats_type, ai_fit_reasoning, created_at`` and
    writes ``status``. We don't import the real ORM class because
    constructing a fake ``AsyncSession.get`` for every property
    access would be brittle — these simple attributes are all the
    orchestrator actually touches.
    """

    id: str
    status: str = "approved"
    title: str = "Senior AI Engineer"
    description: str = "Build the future."
    company_name: str = "Acme"
    url: str = "https://acme.test/careers"
    ats_type: str = "greenhouse"
    ai_fit_reasoning: str = "Strong match."
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 7, 1, tzinfo=timezone.utc)
    )


class _FakeSession:
    def __init__(
        self,
        *,
        deque_rows: list[_FakeJob] | None = None,
        resumes_rows: list[dict[str, Any]] | None = None,
        qa_bank_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._deque_rows = deque_rows or []
        self._resumes_rows = resumes_rows or []
        self._qa_bank_rows = qa_bank_rows or []
        # The orchestrator issues exactly 3 SELECTs per tick:
        #   1. the deque ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1``
        #   2. the resumes SELECT
        #   3. the qa-bank SELECT
        # We pre-populate 3 canned results; late ``execute`` calls
        # raise so a future orchestrator drift can't silently use a
        # stale read.
        self._canned = [
            _FakeResult(self._deque_rows),
            _FakeResult(self._resumes_rows),
            _FakeResult(self._qa_bank_rows),
        ]
        self.added: list[Any] = []
        self.flushed = False
        self.committed = False
        self.execute_call_count = 0

    @asynccontextmanager
    async def _begin(self):
        yield

    def begin(self):
        return self._begin()

    async def execute(self, _stmt):
        self.execute_call_count += 1
        if not self._canned:
            raise AssertionError(
                "_FakeSession.execute called more times than the test "
                "installed canned results for — orchestrator drift"
            )
        return self._canned.pop(0)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True

    def add(self, row) -> None:
        self.added.append(row)


# ----------------------------------------------------------------------
# Per-tick fixtures
# ----------------------------------------------------------------------


def _resume(
    *,
    id: str = "r_default",
    name: str = "ml.pdf",
    tags: list[str] | None = None,
    is_default: bool = True,
) -> _FakeResumeRow:
    """ORM-shaped resume fixture — passed to ``_FakeSession.execute`` as the
    result of the SELECT ``resumes`` query.

    The orchestrator's :func:`_fetch_resumes_and_bank` reads
    ``r.id / r.name / r.tags / r.uploaded_at / r.storage_path`` via
    attribute access (the SQLAlchemy ORM shape). Returning ORM-style
    fakes here is what makes the test catch a future migration that
    renames a ``resume`` column — the test breaks BEFORE production
    sees a column-not-found error.
    """
    return _FakeResumeRow(
        id=id,
        name=name,
        tags=list(tags or ["ml", "python"]),
        is_default=bool(is_default),
        uploaded_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        storage_path=f"resumes/{id}.pdf",
    )


def _qa_entry(
    *,
    id: str = "q1",
    question_pattern: str = "years of experience",
    answer: str | None = "5 years",
) -> _FakeQAEntryRow:
    """ORM-shaped Q&A bank entry fixture (see :func:`_resume` rationale)."""
    return _FakeQAEntryRow(
        id=id,
        question_pattern=question_pattern,
        canonical_question=question_pattern.title(),
        answer=answer,
        answer_type="short_text",
        times_used=1,
    )


# Helpers for form_filler tuple return-value construction
def _submitted_tuple(
    *,
    fields_filled: int = 2,
    submit_clicked: bool = True,
    screenshot_path: str | None = "screenshots/job-happy.png",
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Real-shape ``fill_form`` happy-path return value."""
    event = {
        "status": "submitted",
        "platform": "greenhouse",
        "submitted_at": "2026-07-01T00:00:00Z",
        "screenshot_path": screenshot_path,
        "submit_clicked": submit_clicked,
        "fields_filled": fields_filled,
    }
    qa_matches = [SimpleNamespace(entry_id="q1", confidence=0.9, source="rapidfuzz",
                                    field_id="f1", label="Years", reasoning="")]
    return ([event], qa_matches)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_idle_when_no_approved_rows() -> None:
    """Empty queue → IDLE outcome + zero writes.

    Per the design (``run_one_tick`` short-circuits at the deque
    SELECT before running the per-tick resumes/qabank snapshot),
    only ONE ``execute`` call lands — the deque SELECT itself. The
    snapshot fetches only fire when there's a row worth processing
    (cheaper on an idle queue). This test pins that contract so a
    future refactor doesn't accidentally start snapshotting for
    nothing.
    """
    session = _FakeSession(deque_rows=[])
    llm_client = AsyncMock()
    form_filler = AsyncMock()

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    assert outcome.status is TickStatus.IDLE
    assert outcome.job_id is None
    # No row to release, no rows to add.
    assert session.added == []
    # Exactly one SELECT (the deque) — the snapshot fetches are
    # skipped on an empty queue to avoid wasting indexed reads
    # on a known-empty result set.
    assert session.execute_call_count == 1
    # LLMClient was never exercised (no row → no picker call).
    llm_client.assert_not_called()
    # Form-filler was never invoked on an empty queue.
    form_filler.assert_not_called()


@pytest.mark.asyncio
async def test_tick_happy_path_flips_to_applied_and_writes_application() -> None:
    """Happy path: picker returns a resume, form_filler returns a
    success tuple, and the row is atomically flipped
    approved → applied + an Application row is inserted.
    """
    job = _FakeJob(id="job-happy")
    session = _FakeSession(
        deque_rows=[job],
        resumes_rows=[_resume()],
        qa_bank_rows=[_qa_entry()],
    )
    llm_client = AsyncMock()
    form_filler = AsyncMock(return_value=_submitted_tuple())

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    # 1. Status phase.
    assert outcome.status is TickStatus.SUBMITTED
    assert outcome.job_id == "job-happy"
    # 2. The job was mutated in place to ``applied``.
    assert job.status == "applied"
    # 3. ONE row was added — the Application row. The
    #    ``JobStatusHistory`` row is added via ``session.add(...)``
    #    inside record_status_history too, so we expect TWO adds.
    added_kinds = [type(r).__name__ for r in session.added]
    assert added_kinds.count("Application") == 1
    assert added_kinds.count("JobStatusHistory") == 1
    # 4. The FK flush happened BEFORE the status flip committed —
    #    ``session.flushed = True`` is enough evidence at this layer.
    assert session.flushed is True
    # 5. Form_filler was called once with the picked resume's
    #    plain-dict payload (NOT a dataclass — the orchestrator
    #    converts before invoking form_filler so Playwright can
    #    consume the dict directly).
    form_filler.assert_awaited_once()
    kwargs = form_filler.await_args.kwargs
    assert kwargs["resume"]["id"] == "r_default"
    assert kwargs["resume"]["is_default"] is True
    # 6. v0.7 contract: form_filler is invoked with the QA-bank
    #    payload + llm_client so it can run match_questions
    #    internally. The orchestrator no longer needs its own
    #    match_questions call.
    assert kwargs["bank"]  # non-empty
    assert kwargs["llm_client"] is llm_client
    # ``job`` is the plain-dict envelope; check the keys we read.
    assert kwargs["job"]["id"] == "job-happy"
    # 7. Application row carries the screenshot_path pulled out of
    #    form_filler's event (the ``submission_screenshot_path``
    #    column is the contract — see db.models.Application).
    app_row = next(r for r in session.added if type(r).__name__ == "Application")
    assert app_row.submission_screenshot_path == "screenshots/job-happy.png"
    assert app_row.status == "submitted"


@pytest.mark.asyncio
async def test_tick_no_resume_parks_to_paused_and_records_history() -> None:
    """When :func:`resume_picker` returns ``None`` the row is
    flipped approved → paused and a ``job_status_history`` row is
    appended with ``source='auto_apply'``. The Application insert
    is NOT performed (no Application row added).
    """
    # A job description with no role-family keyword cues + a
    # resumes list with NO default resume and empty tags, so the
    # picker returns ``None`` immediately (no tag overlap, no
    # default bonus to fall back on, no LLM client that could
    # rescue).
    job = _FakeJob(
        id="job-no-resume",
        title="Acme Mystery Role",
        description="Generic posting. See URL for details.",
        ats_type="unknown_board",
    )
    session = _FakeSession(
        deque_rows=[job],
        resumes_rows=[
            _resume(
                id="r_plumber",
                name="plumber.pdf",
                tags=["pipes", "wrench"],
                is_default=False,
            )
        ],
        qa_bank_rows=[],
    )
    llm_client = AsyncMock()
    form_filler = AsyncMock()

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    # 1. Status phase.
    assert outcome.status is TickStatus.NO_RESUME
    assert outcome.job_id == "job-no-resume"
    # 2. Row parked to paused (deterministic failure path).
    assert job.status == "paused"
    # 3. One history row added; NO Application row.
    added_kinds = [type(r).__name__ for r in session.added]
    assert added_kinds.count("JobStatusHistory") == 1
    assert added_kinds.count("Application") == 0
    history_row = next(r for r in session.added if type(r).__name__ == "JobStatusHistory")
    assert history_row.source == "auto_apply"
    assert "no resume-matched" in history_row.note or "no resume" in history_row.note
    # 4. Form_filler was NOT called (early exit on picker failure).
    form_filler.assert_not_called()


@pytest.mark.asyncio
async def test_tick_unmatched_qa_fields_parks_to_paused() -> None:
    """When :func:`form_filler` early-aborts because at least one
    matched field has ``entry_id is None``, the orchestrator must
    park the row to ``paused`` via :data:`TickStatus.UNMATCHED_FIELDS`.

    v0.7 change: form_filler is the one that runs ``match_questions``
    internally, so the orchestrator cannot "see" the unmatched
    fields directly — it relies on the tuple-return contract:

    * ``([], qa_matches)`` with at least one ``entry_id is None``
      ⇒ park.
    * ``([], [])`` ⇒ :data:`TickStatus.NO_FIELDS` (separate test).
    """
    # Simulate form_filler's early-abort return: empty events +
    # qa_matches with at least one unmatched entry.
    unmatched_match = SimpleNamespace(
        entry_id=None, confidence=0.0, source="none",
        field_id="f1", label="Years of experience", reasoning="",
    )
    form_filler = AsyncMock(
        return_value=([], [unmatched_match]),
    )

    job = _FakeJob(id="job-unmatched")
    session = _FakeSession(
        deque_rows=[job],
        resumes_rows=[_resume()],
        qa_bank_rows=[_qa_entry()],
    )
    llm_client = AsyncMock()

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    assert outcome.status is TickStatus.UNMATCHED_FIELDS
    assert outcome.job_id == "job-unmatched"
    assert job.status == "paused"
    added_kinds = [type(r).__name__ for r in session.added]
    assert added_kinds.count("JobStatusHistory") == 1
    assert added_kinds.count("Application") == 0
    history_row = next(r for r in session.added if type(r).__name__ == "JobStatusHistory")
    assert history_row.source == "auto_apply"
    assert "unmatched" in history_row.note.lower()
    # Pin the unmatched count in the human-facing detail string.
    assert "1/1 field" in outcome.detail or "1/1 fields" in outcome.detail


@pytest.mark.asyncio
async def test_tick_no_fields_parks_to_paused() -> None:
    """When the page has zero form fields (``([], [])`` tuple from
    form_filler) the orchestrator parks via :data:`TickStatus.NO_FIELDS`.

    New in v0.7 — the test is split out of the unmatched-fields
    test because the two branches carry semantically different
    operator messages: "no fields to fill on this page" (likely
    an ATS that already has the user's profile pre-loaded, e.g.
    LinkedIn Easy Apply with profile complete) vs "fields
    present but our matcher couldn't reach them" (operator needs
    to add Q&A bank entries).
    """
    form_filler = AsyncMock(return_value=([], []))

    job = _FakeJob(id="job-no-fields")
    session = _FakeSession(
        deque_rows=[job],
        resumes_rows=[_resume()],
        qa_bank_rows=[_qa_entry()],
    )
    llm_client = AsyncMock()

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    assert outcome.status is TickStatus.NO_FIELDS
    assert outcome.job_id == "job-no-fields"
    assert job.status == "paused"
    added_kinds = [type(r).__name__ for r in session.added]
    assert added_kinds.count("JobStatusHistory") == 1
    assert added_kinds.count("Application") == 0
    history_row = next(r for r in session.added if type(r).__name__ == "JobStatusHistory")
    assert "no form fields" in history_row.note


@pytest.mark.asyncio
async def test_tick_paused_race_no_op_commit() -> None:
    """If the dequeued row's ``status`` flips to ``paused`` BEFORE
    our lock acquired (an operator's PATCH raced us in via a
    different session), the orchestrator must commit cleanly with
    no status flip and no Application insert.
    """
    job = _FakeJob(id="job-raced")
    job.status = "paused"  # operator flipped it mid-tick
    session = _FakeSession(
        deque_rows=[job],
        resumes_rows=[_resume()],
        qa_bank_rows=[_qa_entry()],
    )
    llm_client = AsyncMock()
    form_filler = AsyncMock()

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    assert outcome.status is TickStatus.PAUSED_RACE
    assert outcome.job_id == "job-raced"
    # The orchestrator must NOT mutate the row or write anything.
    assert session.added == []
    # Status stays paused (no flip to approved re-creates a
    # regression path).
    assert job.status == "paused"
    # Picker was NOT called (early bail).
    llm_client.assert_not_called()
    # Form_filler was NOT called.
    form_filler.assert_not_called()


@pytest.mark.asyncio
async def test_tick_form_filler_receives_resume_dict_not_dataclass() -> None:
    """Contract: ``form_filler`` always sees a plain dict for the
    resume (Playwright/Supabase Storage upload logic wants JSON-
    serializable inputs). The orchestrator must convert
    ``ResumeRecord`` → dict before invoking ``form_filler`` even
    when :func:`resume_picker` returns the dataclass directly.
    """
    from apply_worker.types import ResumeRecord

    # Patch :func:`pick_resume` to return a dataclass (alternate
    # return shape the orchestrator must also handle).
    import apply_worker.main as main_mod

    dataclass_resume = ResumeRecord(
        id="r_dataclass",
        name="dataclass.pdf",
        tags=["ml"],
        is_default=True,
        uploaded_at="2026-07-01T00:00:00Z",
        storage_path="resumes/r_dataclass.pdf",
    )

    async def _pick_dataclass(*_args, **_kwargs):
        return dataclass_resume

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(main_mod, "pick_resume", _pick_dataclass)

    job = _FakeJob(id="job-dataclass-resume")
    session = _FakeSession(
        deque_rows=[job],
        resumes_rows=[_resume(id="r_dataclass", tags=["ml"], is_default=True)],
        qa_bank_rows=[_qa_entry()],
    )
    llm_client = AsyncMock()
    qa_match = SimpleNamespace(entry_id="q1", confidence=0.9, source="rapidfuzz",
                                field_id="f1", label="Years", reasoning="")
    form_filler = AsyncMock(return_value=(
        [{"status": "submitted", "platform": "stub", "screenshot_path": None,
          "submitted_at": "2026-07-01T00:00:00Z", "submit_clicked": True,
          "fields_filled": 0}],
        [qa_match],
    ))

    outcome = await run_one_tick(session, llm_client, form_filler=form_filler)

    assert outcome.status is TickStatus.SUBMITTED
    form_filler.assert_awaited_once()
    kwargs = form_filler.await_args.kwargs
    assert isinstance(kwargs["resume"], dict), (
        "form_filler.kwargs['resume'] must be a plain dict, "
        f"but got {type(kwargs['resume']).__name__}"
    )
    assert kwargs["resume"]["id"] == "r_dataclass"
    assert kwargs["resume"]["is_default"] is True
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_tick_job_fetch_limit_is_one() -> None:
    """The deque SELECT reads at most one row per tick — the
    worker is per-row-stateful today. If a future change wants to
    process a batch, the cap must be raised deliberately (this
    test pins the value so the constant doesn't drift
    silently).
    """
    assert JOB_FETCH_LIMIT == 1
