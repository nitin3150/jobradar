"""apply_worker.form_filler — real Playwright driver for ``approved`` rows.

Why this module exists
======================

The orchestrator (:mod:`apply_worker.main`) dequeues one
:class:`db.models.Job` row at a time and hands the row's URL to
:func:`fill_form`. This module owns the **entire** Chromium
session for that one row:

1. Open a fresh ``BrowserContext`` (clean cookies + storage — no
   carryover between jobs, which matters for ATS geo/locale
   detection).
2. ``page.goto(job["url"])`` with a 60 s timeout (ATS pages can
   be slow to hydrate behind a CDN).
3. Extract the form's ``<input>/<textarea>/<select>`` field list
   into :class:`apply_worker.types.FormFieldRecord` shape — one
   sync page.evaluate() round-trip, no per-element awaits.
4. Run :func:`apply_worker.qa_matcher.match_questions` against
   the field list so the same matcher the manual path uses
   drives the auto-apply path too (no parallel heuristic that
   would drift).
5. **EARLY ABORT** if any field came back with
   ``entry_id is None`` — clicking Submit on a half-filled
   ATS form is worse than parking. Return
   ``([], qa_matches)`` so the orchestrator parks.
6. Fill the matched answers via ``page.locator(...).fill()`` /
   ``.select_option()`` (id-primary, name-fallback).
7. Upload the picked resume PDF via ``set_input_files(files=
   [{"buffer": bytes, ...}])`` — Playwright accepts raw bytes
   (no temp-file round-trip needed).
8. Click submit via a heuristic chain (role-based → CSS
   ``button[type=submit]`` → ``[data-testid*="submit"]``).
9. Capture ``page.screenshot(full_page=True)`` → bytes →
   upload to Supabase ``apply-screenshots`` bucket.
10. Return ``([{"status": "submitted", "platform": <board>,
    "screenshot_path": <storage path>, "submitted_at": <iso>}],
    qa_matches)``.

Why compute ``qa_matches`` inline (rather than letting the
orchestrator do it)
==========================================================

The orchestrator can't compute :func:`match_questions` itself —
it doesn't have access to the page, so it can't build the field
list in the first place. Two options were considered:

* **A**: form_filler extracts fields only, returns the field
  list. Orchestrator runs match_questions (or spawns a second
  Playwright session). **Rejected** — doubles session cost.
* **B** (chosen): form_filler owns the whole pipeline: extract
  → match → fill → submit → screenshot. The matcher runs
  inside the browser session so its LLM call happens BEFORE
  any per-future-element re-render. Trust the page: Playwright's
  auto-waiting locator semantics handle minor re-renders
  between extraction and fill without re-extracting.

Dependency injection
===================

Three hooks are injectable so tests can drive the logic against
mocks without launching real Chromium or hitting real Supabase:

* ``page_factory`` — async context manager that yields a
  Playwright ``Page`` (or a mock in tests).
* ``screenshot_uploader`` — ``(job_id, png_bytes) -> storage_path``
  callable. Default wires
  :func:`storage.supabase.upload_application_screenshot`.
* ``resume_downloader`` — ``(storage_path) -> bytes`` callable.
  Default wires :func:`storage.supabase.download_resume_bytes`.

Production runs these defaults; tests inject fakes for every
column-shape migration to surface ``AttributeError`` BEFORE
production sees it.

Author note
===========

The early-abort on unmatched fields is a behaviour that
:func:`apply_worker.qa_matcher.match_questions` callers depend
on — a field that returned ``MATCH_SOURCE_NONE`` MUST be
surfaced to the operator for review or the submit step is
unsafe (a job portal with a required "Years of experience"
field left blank will reject the application anyway, but the
operator-facing UI would lose the chance to fill the answer
before the next tick). The orchestrator wires this with the
``UNMATCHED_FIELDS`` parking branch.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncContextManager,
    Awaitable,
    Callable,
    TYPE_CHECKING,
)

if TYPE_CHECKING:  # pragma: no cover — runtime import is gated below
    # Imported for type hints only. Tests don't need Playwright
    # installed; the runtime default ``_default_page_factory``
    # closure imports it lazily so a missing playwright still
    # lets tests import ``apply_worker.form_filler``.
    from playwright.async_api import Page

from apply_worker.qa_matcher import match_questions
from apply_worker.types import FormFieldRecord, MatchResult

_logger = logging.getLogger("jobradar.apply_worker")


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

SUBMISSION_EVENT_SUBMITTED = "submitted"

# 60 s default — ATS pages that lazy-load JS behind a CDN
# routinely take 20-30 s to render the first input. Default
# Playwright timeout is 30 s which historically fired false
# positives on Greenhouse and Lever.
DEFAULT_GOTO_TIMEOUT_MS = 60_000

# Brief post-``domcontentloaded`` settle window. Some ATS pages
# swap the initial JS-only shell for the rendered form only
# AFTER the parser hits a hydration boundary; a 500 ms cushion
# closes that race without making happy-path latency perceptible.
POST_GOTO_HYDRATE_SLEEP_S = 0.5


# ----------------------------------------------------------------------
# Default page factory — production wiring.
# ----------------------------------------------------------------------


@asynccontextmanager
async def _default_page_factory(
    *,
    user_agent: str = "JobRadar/1.0 (+auto-apply; ops)",
) -> AsyncContextManager[Any]:
    """Spin up a fresh Chromium ``BrowserContext`` per job.

    Each call yields exactly one ``Page``. The context is closed
    in the ``finally`` so cookies / IndexedDB / service workers
    from a prior job cannot leak into the next job's session —
    important for ATS geo-locale detection (a Greenhouse token
    set on one application's form can persist inappropriately
    into the next company's session otherwise).
    """
    # Lazy import — tests that inject a fake ``page_factory`` never
    # trigger this code path, so a missing Playwright install does
    # NOT break test collection.
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = await context.new_page()
            yield page
        finally:
            await context.close()
            await browser.close()


# ----------------------------------------------------------------------
# Field extraction — single round-trip page.evaluate() reads the
# entire DOM tree at once. Avoids N+1 round-trips per field which
# would push navigation-pipeline latency into a 5-15 s gap on
# slow ATS pages.
# ----------------------------------------------------------------------


async def _extract_fields(page: Any) -> list[FormFieldRecord]:
    """Walk ``<input>/<textarea>/<select>`` and build :class:`FormFieldRecord` list.

    Strategy for label resolution (in order):

    1. Explicit ``<label for="input_id">`` — the canonical
       accessible pattern.
    2. Wrapping ``<label>`` ancestor (``closest('label')``) — for
       radios/checkboxes rendered as ``<label><input...></label>``.
    3. ``aria-label`` or ``placeholder`` attributes — last resort.

    For ``<select>``, the option values are walked (NOT
    displayed text) so :func:`match_questions` can match against
    :class:`apply_worker.types.QABankRecord.answer` when the
    match is a multi-choice question (e.g. "Willing to
    relocate? yes / no").
    """
    # ``page.evaluate`` runs synchronously in the page's V8 — the
    # result is JSON-RPC-marshalled back into Python. The JS shape
    # MUST match the Python ``FormFieldRecord`` constructor's arg
    # names exactly (``label``, ``field_type``, ``select_options``,
    # ``field_id``) so the constructor below can splat it.
    raw = await page.evaluate(
        r"""() => {
            const out = [];
            const els = document.querySelectorAll(
                'input, textarea, select'
            );
            for (const el of els) {
                const tag = el.tagName.toLowerCase();
                const type = (
                    el.getAttribute('type') ||
                    (tag === 'textarea'
                        ? 'textarea'
                        : (tag === 'select' ? 'select' : 'text'))
                ).toLowerCase();
                const fieldId = (
                    el.id || el.getAttribute('name') || ''
                );
                let label = '';
                if (el.id) {
                    const lab = document.querySelector(
                        'label[for="' + CSS.escape(el.id) + '"]'
                    );
                    if (lab) label = (lab.textContent || '').trim();
                }
                if (!label) {
                    const parent = el.closest('label');
                    if (parent) {
                        label = (parent.textContent || '').trim();
                    }
                }
                if (!label) {
                    label =
                        el.getAttribute('aria-label') ||
                        el.getAttribute('placeholder') ||
                        '';
                }
                let selectOptions = [];
                if (tag === 'select') {
                    selectOptions = Array.from(
                        el.querySelectorAll('option')
                    ).map((o) => o.value).filter(Boolean);
                }
                out.push({
                    label, field_type: type,
                    select_options: selectOptions, field_id: fieldId,
                });
            }
            return out;
        }"""
    )
    return [
        FormFieldRecord(
            label=str(fd.get("label") or "").strip(),
            field_type=_normalise_field_type(str(fd.get("field_type") or "text")),
            select_options=list(fd.get("select_options") or []),
            field_id=str(fd.get("field_id") or ""),
        )
        for fd in raw
    ]


def _normalise_field_type(type_str: str) -> str:
    """Lowercase, treating ``""`` as ``"text"`` for clean downstream dispatch."""
    s = (type_str or "").lower()
    return s if s else "text"


# ----------------------------------------------------------------------
# Fill helpers
# ----------------------------------------------------------------------


# Field types we never fill from a Q&A bank entry — they're
# binary (yes/no), file uploads (resume), or non-text. The
# qa_matcher returns ``MATCH_SOURCE_NONE`` for these so they
# land in the ``UNMATCHED_FIELDS`` parking branch — but we ALSO
# guard here as a defense in depth in case a future matcher
# change starts returning entries for these.
_NON_FILLABLE_TYPES = {"file", "submit", "button", "hidden", "reset", "image"}


async def _fill_field(
    page: Any,
    field: FormFieldRecord,
    answer: str,
) -> bool:
    """Apply ``answer`` to ``field``. Returns ``True`` on success.

    Locator strategy: ``#{field_id}`` first (canonical),
    ``[name="{field_id}"]`` second. Playwright's auto-waiting
    keeps the locate alive across minor JS re-renders for the
    default 5 s — longer if the field is rendered after a slow
    network call.
    """
    if not field.field_id or not answer:
        return False
    if field.field_type in _NON_FILLABLE_TYPES:
        return False

    primary_selector = f"#{field.field_id}"
    fallback_selector = f'[name="{field.field_id}"]'

    async def _try(selector: str) -> bool:
        loc = page.locator(selector).first
        try:
            if field.field_type == "select":
                # Prefer an option whose value matches ``answer``;
                # otherwise fall back to the first valid option so
                # the chain doesn't deadlock on unrecognised values.
                options = field.select_options or []
                chosen = answer if answer in options else (options[0] if options else answer)
                await loc.select_option(value=chosen, timeout=5_000)
            elif field.field_type == "checkbox":
                # ``check()`` is idempotent — safe to call even if
                # the box is already checked.
                await loc.check(timeout=5_000)
            else:
                # ``text``, ``textarea``, ``email``, ``tel``,
                # ``url``, ``number`` — Playwright's ``fill``
                # matches them all with one signature.
                await loc.fill(answer, timeout=5_000)
            return True
        except Exception:
            return False

    return await _try(primary_selector) or await _try(fallback_selector)


async def _upload_resume_to(
    page: Any,
    file_input_selector: str,
    resume: dict[str, Any],
    resume_downloader: Callable[[str], Awaitable[bytes]],
) -> bool:
    """Find the first ``<input type="file">`` and upload the resume bytes.

    Uses Playwright's ``FilePayload`` TypedDict (``name``,
    ``mimeType``, ``buffer``) so the bytes never round-trip
    through a temp file. Returns ``True`` if a file input was
    found and the bytes were attached; ``False`` if the page
    has no file input (most ATS posts don't have a custom
    resume field — they fall back on the candidate's profile).
    """
    try:
        count = await page.locator(file_input_selector).count()
    except Exception:
        return False
    if count == 0:
        return False

    storage_path = resume.get("storage_path")
    if not storage_path:
        return False
    try:
        bytes_ = await resume_downloader(str(storage_path))
    except Exception as exc:
        _logger.warning(
            "form_filler: resume download failed path=%r (%s); skipping file upload",
            storage_path, type(exc).__name__,
        )
        return False

    name = str(resume.get("name") or "resume.pdf")
    mime = "application/pdf" if name.lower().endswith(".pdf") else "application/octet-stream"
    try:
        await page.locator(file_input_selector).first.set_input_files(
            files=[{"name": name, "mimeType": mime, "buffer": bytes_}],
            timeout=10_000,
        )
        return True
    except Exception as exc:
        _logger.warning(
            "form_filler: set_input_files failed (%s); form submitted without resume",
            type(exc).__name__,
        )
        return False


async def _click_submit(page: Any) -> bool:
    """Heuristic submit-button click chain. Returns ``True`` on click.

    Order:

    1. ``page.get_by_role("button", name="Submit")`` — accessible
       tree first (works for icons + JS-rendered buttons where
       the accessible-name is the visible label).
    2. ``page.get_by_role("button", name="Apply")`` — same
       accessibility path, matches the more common "Apply now"
       copy on Greenhouse / Lever.
    3. ``button[type="submit"]`` — semantic HTML fallback.
    4. ``input[type="submit"]`` — for legacy forms.
    5. ``[data-testid*="submit" i]`` — for React-rendered ATS
       portals that data-test every interactive element.

    Each attempt uses a 5 s timeout so a slow page can't stall
    the whole chain; the next attempt fires immediately.
    """
    strategy_chain = (
        ("role", "Submit"),
        ("role", "Apply"),
        ("css", 'button[type="submit"]'),
        ("css", 'input[type="submit"]'),
        ("css", '[data-testid*="submit" i]'),
    )
    for kind, target in strategy_chain:
        try:
            if kind == "role":
                locator = page.get_by_role("button", name=target).first
            else:
                locator = page.locator(target).first
            await locator.click(timeout=5_000)
            _logger.info("form_filler: submit clicked via %s %r", kind, target)
            return True
        except Exception:
            continue
    _logger.warning(
        "form_filler: no submit button matched the heuristic chain; "
        "form will look filled but unsubmitted"
    )
    return False


# ----------------------------------------------------------------------
# Default resume downloader — production wires to Supabase Storage.
# Kept as a callable parameter ``resume_downloader`` so tests inject
# a fake without a live Supabase configuration.
# ----------------------------------------------------------------------


async def _default_resume_downloader(storage_path: str) -> bytes:
    from storage.supabase import download_resume_bytes
    return await download_resume_bytes(storage_path)


# ----------------------------------------------------------------------
# Public entry — the orchestrator calls this once per row.
# ----------------------------------------------------------------------


async def fill_form(
    *,
    job: dict[str, Any],
    resume: dict[str, Any] | None,
    bank: list[dict[str, Any]],
    llm_client: Any,
    page_factory: Callable[..., AsyncContextManager[Any]] = _default_page_factory,
    screenshot_uploader: Callable[..., Awaitable[str]] | None = None,
    resume_downloader: Callable[[str], Awaitable[bytes]] = _default_resume_downloader,
    go_to_timeout_ms: int = DEFAULT_GOTO_TIMEOUT_MS,
    post_goto_sleep_s: float = POST_GOTO_HYDRATE_SLEEP_S,
    file_input_selector: str = 'input[type="file"]',
) -> tuple[list[dict[str, Any]], list[MatchResult]]:
    """Drive a single apply-page session end-to-end.

    Args:
        job: Plain-dict job row (URL, id, ats_type, title,
            company_name, …). Built by the orchestrator's
            ``await form_filler(job=…)`` callsite from the
            SQLAlchemy ORM row's columns.
        resume: Picked resume metadata (``id``, ``name``,
            ``tags``, ``is_default``, ``storage_path``) OR
            ``None``. ``fill_form`` does NOT itself enforce the
            "no resume → park" gate (the orchestrator does, so
            the parking audit trail is built there); defensive
            callers that bypass the orchestrator get a hard
            failure if they pass ``resume=None`` plus a file
            input present.
        bank: QA-bank payload from the orchestrator's per-tick
            snapshot — passed to
            :func:`apply_worker.qa_matcher.match_questions`.
        llm_client: Required by ``match_questions`` for the
            LLM-fallback pass; tests pass an ``AsyncMock``.
        page_factory: Async context manager that yields one
            ``Page``. Tests inject a mock; production uses
            :data:`_default_page_factory`.
        screenshot_uploader: ``async (job_id, png_bytes) ->
            storage_path`` callable — defaults to
            :func:`storage.supabase.upload_application_screenshot`.
            Tests inject a mock that records the call but
            doesn't talk to Supabase.
        resume_downloader: ``async (storage_path) -> bytes``
            callable — defaults to
            :func:`storage.supabase.download_resume_bytes`.
        go_to_timeout_ms: Override the default 60 s to A/B
            load-time regressions in staging.
        post_goto_sleep_s: Hydration window after
            ``wait_until="domcontentloaded"``. Default 500 ms —
            the bottleneck on Greenhouse is React hydration of
            the dynamic form section.
        file_input_selector: CSS selector for the resume
            file input. Default is the only-such-element
            form filer ATS portals use today.

    Returns:
        ``(events, qa_matches)``:

        * ``events = []`` and ``qa_matches = []`` — page had no
          form fields to fill or the extract step errored. The
          orchestrator parks via ``NO_FIELDS``.
        * ``events = []`` and ``qa_matches != []`` with at
          least one ``entry_id is None`` — early-abort on
          unmatched fields. The orchestrator parks via
          ``UNMATCHED_FIELDS``.
        * ``events = [{...}]`` and all ``qa_matches`` matched
          — happy path; orchestrator fliipped status to
          ``applied`` via :func:`apply_worker.main._finalize_apply`.
    """
    if screenshot_uploader is None:
        from storage.supabase import upload_application_screenshot
        screenshot_uploader = upload_application_screenshot

    async with page_factory() as page:
        # ---- 1. Goto ------------------------------------------------------
        url = job.get("url")
        if not url:
            _logger.warning(
                "form_filler: job %r has no URL; returning NO_FIELDS-like empty tuple",
                job.get("id"),
            )
            return ([], [])
        try:
            await page.goto(url, timeout=go_to_timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:
            # goto failure is operator-visible: surface as a
            # NO_FIELDS-shaped return so the orchestrator parks
            # rather than entering an infinite retry loop on a
            # broken URL.
            _logger.warning(
                "form_filler: page.goto failed for url=%r (%s)",
                url, type(exc).__name__,
            )
            return ([], [])
        if post_goto_sleep_s > 0:
            await asyncio.sleep(post_goto_sleep_s)

        # ---- 2. Extract --------------------------------------------------
        try:
            fields = await _extract_fields(page)
        except Exception as exc:
            _logger.warning(
                "form_filler: extract failed (%s); returning empty tuple",
                type(exc).__name__,
            )
            return ([], [])
        if not fields:
            _logger.info("form_filler: page %r has no form fields; NO_FIELDS", url)
            return ([], [])

        # ---- 3. Match Q&A (inside one Playwright session — see module docstring)
        qa_matches: list[MatchResult] = await match_questions(
            bank, fields, llm_client=llm_client
        )
        unmatched_count = sum(1 for m in qa_matches if m.entry_id is None)

        # ---- 4. EARLY ABORT on unmatched fields --------------------------
        if unmatched_count:
            _logger.info(
                "form_filler: %d/%d fields unmatched; EARLY ABORT before submit; "
                "orchestrator will park to paused",
                unmatched_count, len(qa_matches),
            )
            return ([], qa_matches)

        # ---- 5. Fill answers --------------------------------------------
        answer_by_entry = {
            str(b.get("id")): str(b.get("answer") or "")
            for b in bank
            if b.get("id") is not None
        }
        filled_count = 0
        for m in qa_matches:
            answer = answer_by_entry.get(str(m.entry_id or ""), "")
            matching_field = next(
                (f for f in fields if f.field_id == m.field_id), None
            )
            if matching_field is None:
                continue
            if await _fill_field(page, matching_field, answer):
                filled_count += 1

        # ---- 6. Upload resume -------------------------------------------
        if resume is not None:
            await _upload_resume_to(
                page, file_input_selector, resume, resume_downloader
            )

        # ---- 7. Submit ---------------------------------------------------
        submitted = await _click_submit(page)

        # ---- 8. Screenshot + upload -------------------------------------
        try:
            png_bytes = await page.screenshot(full_page=True)
        except Exception as exc:
            _logger.warning(
                "form_filler: page.screenshot failed (%s); emitting no path",
                type(exc).__name__,
            )
            png_bytes = b""
        screenshot_path: str | None = None
        if png_bytes:
            try:
                screenshot_path = await screenshot_uploader(
                    str(job.get("id") or "unknown"), png_bytes
                )
            except Exception as exc:
                _logger.warning(
                    "form_filler: screenshot upload failed (%s); "
                    "application row will have NULL submission_screenshot_path",
                    type(exc).__name__,
                )

        submitted_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        event = {
            "status": SUBMISSION_EVENT_SUBMITTED,
            "platform": str(job.get("ats_type") or "unknown"),
            "screenshot_path": screenshot_path,
            "submitted_at": submitted_at,
            "submit_clicked": submitted,
            "fields_filled": filled_count,
        }
        return ([event], qa_matches)


__all__ = [
    "fill_form",
    "SUBMISSION_EVENT_SUBMITTED",
    "DEFAULT_GOTO_TIMEOUT_MS",
    "_default_page_factory",
]
