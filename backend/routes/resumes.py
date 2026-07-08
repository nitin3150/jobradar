"""Resumes router — server-side resume storage for the React ``ResumesModal``.

In-memory only: bytes are held in :data:`_RESUME_BYTES` and metadata in
:data:`_RESUMES_DB`. Survives requests but not process restarts. Swap
for a blob-store-backed implementation (S3 / GCS / Postgres ``bytea``)
when the persistence layer lands — the public shape is stable.

Wire shape mirrors what ``frontend/src/hooks/useResumes.js`` reads:

* ``GET /api/resumes`` → ``{"resumes": [...], "total": int}`` —
  frontend awaits ``r.data.resumes``.
* ``POST /api/resumes`` accepts ``multipart/form-data`` with three
  fields: ``file`` (``UploadFile``, required), ``tags`` (comma-separated
  ``str``), ``is_default`` (``"true"`` / ``"false"``, parsed by FastAPI
  via :class:`fastapi.Form`). The frontend's :func:`uploadResume`
  helper in ``frontend/src/api/resumes.js`` documents why the request
  needs ``Content-Type: undefined`` — without that, axios's default
  ``application/json`` header reaches FastAPI as text and trips a 422
  from the multipart parser. The 10 MiB cap mirrors the frontend's
  client-side ``MAX_BYTES`` check.
* ``PATCH /api/resumes/{id}`` — JSON ``{"tags": [...], "is_default": bool}``,
  either field optional. ``tags`` is normalized server-side (trim /
  dedupe / drop blanks) so the response drives React Query cache
  reconciliation in ``useResumes``.
* ``DELETE /api/resumes/{id}`` — 204, no body.
* ``GET /api/resumes/{id}/download`` — streams the stored bytes back
  via :class:`fastapi.Response` (raw bytes + ``Content-Disposition``)
  rather than :class:`fastapi.responses.FileResponse` so the demo
  doesn't round-trip through a temp file on disk. The original
  filename is preserved in ``Content-Disposition``. Used by the
  ResumesModal's anchor ``href`` — clicking "Download" opens a new
  tab.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Path, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


router = APIRouter()


# Mirror the frontend ``MAX_BYTES`` cap from ResumesModal.jsx so a
# manipulated client can't smuggle in unbounded bytes.
MAX_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Models — field names match the React hook and component attributes
# (``resume.id``, ``resume.name``, ``resume.size_bytes``,
# ``resume.uploaded_at``, ``resume.tags``, ``resume.is_default``).
# ---------------------------------------------------------------------------
class Resume(BaseModel):
    id: str
    name: str
    size_bytes: int
    uploaded_at: str   # ISO 8601 UTC
    tags: list[str] = Field(default_factory=list)
    is_default: bool = False


class ResumeListResponse(BaseModel):
    resumes: list[Resume]
    total: int


class ResumePatch(BaseModel):
    tags: list[str] | None = None
    is_default: bool | None = None


# ---------------------------------------------------------------------------
# Seeded metadata — ``_RESUMES_BYTES`` is empty at import so the demo
# ``GET /download`` still works for at least one record. Real bytes ship
# from client uploads; the seed is metadata-only because storing fake
# bytes would produce nonsensical PDFs on the frontend "Download" click
# during demo.
# ---------------------------------------------------------------------------
_NOW_SEED = "2026-01-08T00:00:00Z"

_SEED_RECORDS: list[dict] = [
    {
        "id": "r_seed_1",
        "name": "ml-engineer.pdf",
        "size_bytes": 218_000,
        "uploaded_at": _NOW_SEED,
        "tags": ["ml", "python", "pytorch"],
        "is_default": True,
    },
    {
        "id": "r_seed_2",
        "name": "backend-api.pdf",
        "size_bytes": 195_000,
        "uploaded_at": "2026-01-05T00:00:00Z",
        "tags": ["backend", "fastapi", "python"],
        "is_default": False,
    },
]


_RESUMES_DB: dict[str, dict] = {}
_RESUME_BYTES: dict[str, bytes] = {}


def _seed() -> None:
    """Reset :data:`_RESUMES_DB` + :data:`_RESUME_BYTES` from canonical seed.

    Deep-copies the metadata records so a PATCH mutation cannot bleed
    back into ``_SEED_RECORDS`` between tests.
    """
    _RESUMES_DB.clear()
    _RESUME_BYTES.clear()
    for rec in _SEED_RECORDS:
        _RESUMES_DB[rec["id"]] = copy.deepcopy(rec)


# Initialize at import time so the routes have live data immediately.
_seed()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_tags(raw: list[str]) -> list[str]:
    """Trim / drop blanks / dedupe while preserving original order.

    Mirrors the ``useResumes.onSuccess`` reconcilation: React Query's
    cache reflects the server's response, so this server-side pass is
    the canonical normalization step.
    """
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        v = (t or "").strip()
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _parse_form_tags(raw: str | None) -> list[str]:
    """Split a comma-separated ``tags`` form field into a list."""
    if not raw:
        return []
    return [t for t in (s.strip() for s in raw.split(",")) if t]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=ResumeListResponse)
def list_resumes() -> ResumeListResponse:
    """Return all resumes, newest first."""
    # Sort by uploaded_at DESC using ISO-8601 string comparison — works
    # because all timestamps end in ``Z`` (UTC) and the field always has
    # the same precision.
    records = sorted(
        _RESUMES_DB.values(),
        key=lambda r: r["uploaded_at"],
        reverse=True,
    )
    return ResumeListResponse(
        resumes=[Resume(**r) for r in records],
        total=len(records),
    )


@router.post("", response_model=Resume, status_code=201)
async def upload_resume(
    file: UploadFile = File(..., description="Resume file (PDF/DOC/DOCX/TXT/MD)."),
    tags: str | None = Form(default=None, description="Comma-separated tag list."),
    is_default: bool = Form(default=False, description='When "true", mark as default resume.'),
) -> Resume:
    # Read once into memory so we can size-check + store atomically.
    contents = await file.read()
    if len(contents) > MAX_BYTES:
        # ``413 Payload Too Large`` so the frontend's existing
        # ``e?.response?.status === 413`` branch in ResumesModal surfaces
        # a friendly error rather than collapsing to "Request failed".
        raise HTTPException(
            status_code=413,
            detail=f"resume over the {MAX_BYTES // (1024 * 1024)} MB cap",
        )

    resume_id = uuid4().hex
    raw_tags = _parse_form_tags(tags)
    tags_list = _normalize_tags(raw_tags)

    # Single default resume invariant: when ``is_default`` is True,
    # demote any previously-default record. Mirrors the React hook's
    # "Default checkbox" UX — only one resume is the active default.
    if is_default:
        for r in _RESUMES_DB.values():
            if r["is_default"] and r["id"] != resume_id:
                r["is_default"] = False

    record = {
        "id": resume_id,
        "name": file.filename or "resume.pdf",
        "size_bytes": len(contents),
        "uploaded_at": _now_iso(),
        "tags": tags_list,
        "is_default": is_default,
    }
    _RESUMES_DB[resume_id] = record
    _RESUME_BYTES[resume_id] = contents
    return Resume(**record)


@router.patch("/{resume_id}", response_model=Resume)
def patch_resume(
    payload: ResumePatch,
    resume_id: str = Path(min_length=1, max_length=64),
) -> Resume:
    rec = _RESUMES_DB.get(resume_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"resume {resume_id!r} not found")

    if payload.tags is not None:
        rec["tags"] = _normalize_tags(payload.tags)

    if payload.is_default is not None:
        if payload.is_default:
            # Enforce the single-default invariant when flipping ON.
            for r in _RESUMES_DB.values():
                if r["is_default"] and r["id"] != resume_id:
                    r["is_default"] = False
        rec["is_default"] = payload.is_default

    return Resume(**rec)


@router.delete("/{resume_id}", status_code=204)
def delete_resume(resume_id: str = Path(min_length=1, max_length=64)) -> None:
    rec = _RESUMES_DB.pop(resume_id, None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"resume {resume_id!r} not found")
    _RESUME_BYTES.pop(resume_id, None)
    # ``return None`` with ``status_code=204`` is the FastAPI idiom for
    # "no response body" — TestClient exposes it as ``r.content == b''``.


@router.get("/{resume_id}/download")
def download_resume(resume_id: str = Path(min_length=1, max_length=64) ):
    rec = _RESUMES_DB.get(resume_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"resume {resume_id!r} not found")
    contents = _RESUME_BYTES.get(resume_id)
    if contents is None:
        # Seeded metadata has no bytes — give the frontend a friendly
        # 410 Gone so it doesn't look like a transient backend error.
        raise HTTPException(
            status_code=410,
            detail=f"resume {resume_id!r} has no stored bytes (seeded metadata only)",
        )
    # ``Response`` is used instead of ``FileResponse`` so the demo
    # doesn't round-trip through a temp file on disk; the saved bytes
    # are streamed back verbatim with the original filename in
    # ``Content-Disposition`` so the anchor ``href`` opens a download.
    from fastapi import Response
    return Response(
        content=contents,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{rec["name"]}"'},
    )
