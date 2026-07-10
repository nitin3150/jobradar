"""Supabase Storage helper — async wrapper around the (sync) supabase-py SDK.

Why this module exists
======================

The official ``supabase`` Python SDK is **synchronous**; calling
``client.storage.from_(...).upload(...)`` directly from a FastAPI route
handler would block the event loop for the duration of the HTTPS
round-trip — unacceptable on a request-per-task backend.

This module owns the SDK client (singleton, lazy-initialised from env)
and exposes async helpers used by the resume route AND the
``apply_worker`` form-filler:

* :func:`upload_resume_bytes` — uploads bytes to the ``resumes`` bucket
  under a path of the form ``<resume_id>.<ext>``; returns the storage
  path that the ``resumes.storage_path`` column persists.
* :func:`download_resume_bytes` — fetches bytes back so the
  ``GET /api/resumes/{id}/download`` route returns a real file body,
  not a 410 Gone against seeded metadata.
* :func:`delete_resume_bytes` — used by ``DELETE /api/resumes/{id}``
  so the upload+view flow round-trips cleanly.
* :func:`upload_application_screenshot` — uploads PNG screenshots of
  the apply page (taken by :mod:`apply_worker.form_filler`) to the
  ``apply-screenshots`` bucket; returns the storage path the
  orchestrator persists on ``applications.submission_screenshot_path``.

Configuration
=============

Both helpers read ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY``
from the environment at module-import time. If either is missing, the
client is left ``None`` and every helper raises :class:`RuntimeError`
with a clear remediation message — instead of failing opaquely inside
the SDK.

**The service-role key is server-side only.** Never expose it to the
React frontend. The frontend always goes through the FastAPI proxy
which uses this key on behalf of the operator.

Bucket layout
=============

* ``resumes`` bucket (private) — PDF/DOCX/TXT/Markdown resume objects
  stored as ``<resume_id>.<ext>`` so the on-the-wire ``resumes.id``
  (a UUID) is the object name and the ``storage_path`` column
  round-trips back to ``<resume_id>.<ext>`` on read.
* ``apply-screenshots`` bucket (private) — PNG screenshots of the
  apply page captured by :mod:`apply_worker.form_filler` after
  clicking Submit. Stored as ``<job_id>.png`` so the on-the-wire
  ``applications.job_id`` (a UUID) is the object name. Kept in a
  separate bucket so a future privacy review of the resume download
  policy doesn't accidentally also lock down screenshots (or vice
  versa).
* RLS is **off** at the Storage layer for both buckets for the
  same reason it's off at the Postgres layer (single-user demo).
  The helper goes through the service-role key which bypasses
  Storage policies by design.

SDK API used
============

``supabase>=2.4.0`` ships ``client.storage.from_(bucket)`` which
exposes ``.upload(file, path, file_options)`` and ``.download(path)``.
``file_options`` carries multipart metadata (``content-type``,
``upsert``). Errors propagate as :class:`StorageApiError` /
:class:`StorageError`; we surface those as :class:`RuntimeError` with
the cause preserved so the route's 5xx log line is informative.

Concurrency
===========

Both helpers dispatch the blocking SDK call to the running loop's
default thread pool via :func:`fastapi.concurrency.run_in_threadpool`
— the awaited ``coroutine`` returns the SDK result without holding
the event loop hostage. This is the same pattern
``starlette.concurrency.run_in_threadpool`` uses for filesystem I/O.
"""
from __future__ import annotations

import os
from typing import Final

from fastapi.concurrency import run_in_threadpool

try:
    # ``supabase`` is optional for unit tests that don't exercise the
    # uploads route. The import is local so a missing dep turns into
    # ``_client = None`` instead of breaking every test fixture.
    from supabase import Client, create_client
except ImportError:  # pragma: no cover — exercised only without supabase installed
    Client = None  # type: ignore[assignment]
    create_client = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration — read once at import. The values are intentionally NOT
# exposed via attribute so they cannot leak into logs / responses
# accidentally.
# ---------------------------------------------------------------------------
_SUPABASE_URL: Final[str | None] = os.environ.get("SUPABASE_URL") or None
_SUPABASE_SERVICE_ROLE_KEY: Final[str | None] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None
)


# ---------------------------------------------------------------------------
# Lazy SDK client. Created only when both env vars are present so the
# in-memory routes that don't touch Storage can keep running without
# the operator setting up Supabase on a quick demo.
# ---------------------------------------------------------------------------
_client: Client | None = None
if (
    _SUPABASE_URL is not None
    and _SUPABASE_SERVICE_ROLE_KEY is not None
    and create_client is not None
):
    _client = create_client(_SUPABASE_URL, _SUPABASE_SERVICE_ROLE_KEY)


# Bucket name — single private bucket for resume objects. Exposed as a
# constant so the route layer doesn't pass a string literal every call.
RESUMES_BUCKET: Final[str] = "resumes"

# New private bucket for apply-worker screenshots. The form_filler
# captures a full-page PNG of the apply page AFTER submit, uploads
# it here, and persists the storage path on
# ``applications.submission_screenshot_path`` via the orchestrator.
# The render of the screenshot at ``GET /api/applications/{id}/screenshot``
# is a followup — the bytes live in Supabase Storage today and the
# route handler can stream them once a ``Storage ADMIN`` policy
# passes an audit. Keeping this bucket separate from ``resumes``
# keeps Storage RLS reviews scoped: a future privacy review that
# wants to tighten resume downloads doesn't accidentally also lock
# out screenshot reads (or vice versa).
APPLY_SCREENSHOTS_BUCKET: Final[str] = "apply-screenshots"


def _ensure_client() -> Client:
    """Raise with a clear remediation message if the SDK isn't configured.

    This separates the ``RuntimeError`` (configuration issue) from the
    upstream ``StorageError`` / ``StorageApiError`` (real upload issue)
    so the operator's log greppability is preserved.
    """
    if _client is None:
        raise RuntimeError(
            "Supabase client is not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in the environment (see "
            "`.env.example` for the exact pattern) and restart the process."
        )
    return _client


def _infer_extension(content_type: str | None, filename: str | None) -> str:
    """Pick a sensible file extension for the storage object name.

    Prefers the ``content_type`` mapping (``application/pdf`` → ``.pdf``
    etc.) and falls back to the trailing suffix of ``filename`` so the
    storage object's name matches the original upload's extension.
    """
    if content_type:
        normalised = content_type.split(";", 1)[0].strip().lower()
        mapping = {
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "text/plain": ".txt",
            "text/markdown": ".md",
        }
        if normalised in mapping:
            return mapping[normalised]
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ".bin"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
async def upload_resume_bytes(
    resume_id: str,
    file_bytes: bytes,
    *,
    content_type: str | None = None,
    filename: str | None = None,
) -> str:
    """Upload ``file_bytes`` to the ``resumes`` bucket under ``resume_id``.

    Returns the storage path (a relative bucket-key string like
    ``"<uuid>.pdf"``) that the route should persist into
    ``resumes.storage_path`` so the read side can mirror it back.

    The upload is idempotent: re-uploads use ``upsert=true`` so a
    retry-after-network-flap does not 409.
    """
    client = _ensure_client()
    extension = _infer_extension(content_type, filename)
    path = f"{resume_id}{extension}"

    def _do_upload() -> str:
        client.storage.from_(RESUMES_BUCKET).upload(
            file=file_bytes,
            path=path,
            file_options={
                "content-type": content_type or "application/octet-stream",
                # supabase-py ≥2.4 expects a boolean here, not the string
                # form. The earlier `"true"` recompiles fine on recent
                # versions but is silently ignored — the upload then
                # 409s instead of overwriting.
                "upsert": True,
            },
        )
        return path

    return await run_in_threadpool(_do_upload)


async def download_resume_bytes(storage_path: str) -> bytes:
    """Fetch the bytes for ``storage_path`` from the ``resumes`` bucket.

    Used by ``GET /api/resumes/{id}/download`` to stream real bytes.
    For seeded metadata rows whose ``storage_path`` was never written,
    the route handler should detect the empty case BEFORE calling this
    helper — this function will raise a :class:`RuntimeError` if the
    path doesn't exist, which is the right 5xx signal.
    """
    client = _ensure_client()

    def _do_download() -> bytes:
        return client.storage.from_(RESUMES_BUCKET).download(storage_path)

    return await run_in_threadpool(_do_download)


async def delete_resume_bytes(storage_path: str) -> None:
    """Remove a resume object — used by ``DELETE /api/resumes/{id}``.

    Errors from the SDK propagate (the route should turn them into
    5xx or surface a soft-delete state depending on its policy).
    """
    client = _ensure_client()

    def _do_delete() -> None:
        client.storage.from_(RESUMES_BUCKET).remove([storage_path])

    return await run_in_threadpool(_do_delete)


async def upload_application_screenshot(
    job_id: str,
    png_bytes: bytes,
    *,
    filename: str | None = None,
) -> str:
    """Upload a PNG screenshot of the apply page to the ``apply-screenshots`` bucket.

    Used by :func:`apply_worker.form_filler.fill_form` after
    ``page.screenshot(full_page=True)``. Returns the storage
    path (``<job_id>.png``) that the orchestrator persists on
    ``applications.submission_screenshot_path`` so
    :class:`db.models.Application` round-trips back to the bytes
    via ``GET /api/applications/{id}/screenshot`` (a future route).

    The upload is idempotent (``upsert=true``) — re-running the
    worker on the same job overwrites the prior screenshot rather
    than 409-ing. The :class:`RuntimeError` from ``_ensure_client``
    propagates so the orchestrator can park the row on a missing
    configuration rather than silently dropping the bytes.
    """
    client = _ensure_client()
    extension = ".png"
    path = filename or f"{job_id}{extension}"

    def _do_upload() -> str:
        client.storage.from_(APPLY_SCREENSHOTS_BUCKET).upload(
            file=png_bytes,
            path=path,
            file_options={
                "content-type": "image/png",
                # supabase-py ≥2.4 expects a boolean here, not the string
                # form. The earlier ``"true"`` recompiles fine on recent
                # versions but is silently ignored — the upload then
                # 409s instead of overwriting. Same pattern as
                # ``upload_resume_bytes``.
                "upsert": True,
            },
        )
        return path

    return await run_in_threadpool(_do_upload)


__all__ = [
    "RESUMES_BUCKET",
    "APPLY_SCREENSHOTS_BUCKET",
    "upload_resume_bytes",
    "download_resume_bytes",
    "delete_resume_bytes",
    "upload_application_screenshot",
]
