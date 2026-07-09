"""Profile router — exposes the candidate profile to the front-end.

Wire shape:

* ``GET /api/profile`` → returns the current :class:`services.profile_service.Profile`
  (loaded from ``config/profile.yml`` or, if missing, the example file).
  The React ``JobBoard`` / ``ApplicationTracker`` will eventually read
  this to render the operator's target-roles + narrative without
  calling the LLM scoring loop.
* ``POST /api/profile/regenerate`` → runs the resume → profile
  extractor as a background task. Accepts an optional
  ``{"resume_id": "..."}`` body; if omitted, the default resume
  (``is_default=True``) is used. Returns ``202 Accepted`` with the
  resume id that was enqueued.

Why a background task + 202
===========================

Profile extraction calls an LLM and can take 3-15 seconds depending
on the chain (NVIDIA primary, Groq fallback) and the resume's
complexity. The route returns 202 immediately so the front-end
gets a snappy "regenerate queued" acknowledgement while the
extraction runs server-side. The front-end can poll
``GET /api/profile`` (or watch for an SSE later) to detect
completion; v1 logs the result server-side and the operator
checks logs / ``config/profile.yml`` for the updated content.

Storage
=======

Reads + writes go through :mod:`services.profile_service` so the
Pydantic model + YAML serializer live in one place. The route is
a thin shell that maps HTTP → service call.

Resume bytes access
===================

The regenerate endpoint needs to read the raw bytes of an existing
resume. Those bytes live in the in-memory ``_RESUME_BYTES`` dict
owned by :mod:`routes.resumes`. We import lazily inside the route
body to avoid a circular import at module load time
(``routes.resumes`` already imports from ``services.profile_service``,
so a top-level ``from routes.resumes import _RESUME_BYTES`` here
would form the cycle). Lazy import runs at request time, by which
point both modules are fully loaded.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from services.profile_service import (
    Profile,
    _run_profile_extraction_after_upload,
    load_profile,
)


router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class RegenerateRequest(BaseModel):
    """Request body for ``POST /api/profile/regenerate``.

    ``resume_id`` is optional. When omitted, the route picks the
    default resume (``is_default=True``) from the in-memory store.
    A 400 is returned if no default exists and the operator didn't
    pin a specific resume id — the alternative (silently falling
    back to the most recent upload) is the kind of "helpful" default
    that wastes LLM tokens on the wrong resume.
    """

    resume_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=Profile)
def get_profile() -> Profile:
    """Return the current candidate profile.

    Reads from ``config/profile.yml`` (operator's own) or falls
    back to ``config/profile.example.yml`` (template) when the
    operator hasn't created their own yet. An empty ``Profile()``
    is returned when neither file exists — the React UI sees an
    empty object and can render a "Upload a resume to populate
    this" prompt.
    """
    return load_profile()


@router.post("/regenerate", status_code=202)
async def regenerate_profile(
    payload: RegenerateRequest | None = None,
    background_tasks: BackgroundTasks = ...,
) -> dict:
    """Re-run the resume → profile extractor on an existing resume.

    Args:
        payload: Optional ``{"resume_id": "..."}`` body. When
            ``resume_id`` is set, that resume is used. When
            omitted, the route picks the default resume
            (``is_default=True``); a 400 is raised if no default
            exists.
        background_tasks: FastAPI's background-task runner. The
            actual extraction happens in
            :func:`services.profile_service._run_profile_extraction_after_upload`
            so the route returns 202 immediately.

    Returns:
        ``{"status": "queued", "resume_id": "..."}`` so the
        front-end can display "Regenerating profile from <name>…"
        and surface a refresh button when the user comes back.

    Raises:
        400: No ``resume_id`` in the body AND no default resume
            exists in the store. The operator needs to either pin
            a specific resume or mark one as default before the
            endpoint can pick a source.
        404: The requested ``resume_id`` doesn't exist in the
            store (typo / deleted).
        410: The requested ``resume_id`` exists but has no stored
            bytes (one of the seeded metadata-only records). The
            operator should upload a real resume.
    """
    # Lazy import — see the module docstring for the
    # routes ↔ routes circular-import rationale. By request time
    # both modules are fully loaded, so this is just a dict lookup.
    from routes.resumes import _RESUME_BYTES, _RESUMES_DB

    payload = payload or RegenerateRequest()
    if payload.resume_id:
        resume_id = payload.resume_id
    else:
        # Pick the default resume (is_default=True). The
        # ``next(..., None)`` falls through to the 400 below if
        # no resume is marked default.
        default = next(
            (rec for rec in _RESUMES_DB.values() if rec.get("is_default")),
            None,
        )
        if default is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no default resume to regenerate from; pass "
                    "'resume_id' in the body or mark a resume as "
                    "default via PATCH /api/resumes/{id}"
                ),
            )
        resume_id = default["id"]

    rec = _RESUMES_DB.get(resume_id)
    if rec is None:
        raise HTTPException(
            status_code=404, detail=f"resume {resume_id!r} not found"
        )

    contents = _RESUME_BYTES.get(resume_id)
    if contents is None:
        # Mirrors the GET /download 410 contract — a seeded
        # metadata record has no bytes. The front-end can show
        # "this is a demo record; upload a real resume".
        raise HTTPException(
            status_code=410,
            detail=(
                f"resume {resume_id!r} has no stored bytes (seeded "
                f"metadata only) — upload a real resume to use as "
                f"the extraction source"
            ),
        )

    background_tasks.add_task(
        _run_profile_extraction_after_upload,
        resume_id,
        contents,
        rec["name"],
    )
    return {"status": "queued", "resume_id": resume_id}
