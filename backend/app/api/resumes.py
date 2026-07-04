"""Resume CRUD endpoints — multi-part upload, per-resume tagging, secure download."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import MAX_RESUME_BYTES, MAX_TAGS_PER_RESUME, Resume
from app.resumes.extract import extract_text
from app.schemas.resume import ResumeListResponse, ResumeResponse, ResumeUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/resumes", tags=["resumes"])

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
}

# Map extension -> content_type so we can fix the user-provided mime when the
# browser sends a generic "application/octet-stream".
_EXT_TO_CT = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def _resume_disk_path(storage_filename: str) -> Path:
    base = Path(settings.resume_storage_dir)
    base.mkdir(parents=True, exist_ok=True)
    # storage_filename is a UUID + ext chosen server-side — never trust user input here.
    return base / storage_filename


def _to_response(r: Resume) -> ResumeResponse:
    return ResumeResponse(
        id=r.id,
        name=r.name,
        content_type=r.content_type,
        size_bytes=r.size_bytes,
        tags=list(r.tags or []),
        is_default=r.is_default,
        uploaded_at=r.uploaded_at,
        download_url=f"/api/resumes/{r.id}/download",
    )


def _normalize_tags(raw: str | None, items: list[str] | None) -> list[str]:
    """Accept either repeated multipart `tags` fields or a single comma string."""
    out: list[str] = []
    if raw:
        out.extend(t.strip() for t in raw.split(","))
    if items:
        out.extend(t.strip() for t in items if t and t.strip())
    # lower-case, dedupe, drop blanks, cap
    seen: set[str] = set()
    normalized: list[str] = []
    for t in out:
        norm = t.lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        normalized.append(norm)
        if len(normalized) >= MAX_TAGS_PER_RESUME:
            break
    return normalized


@router.get("", response_model=ResumeListResponse)
async def list_resumes(db: AsyncSession = Depends(get_db)) -> ResumeListResponse:
    result = await db.execute(
        select(Resume).order_by(Resume.is_default.desc(), Resume.uploaded_at.desc())
    )
    return ResumeListResponse(resumes=[_to_response(r) for r in result.scalars().all()])


@router.get("/{resume_id}", response_model=ResumeResponse)
async def get_resume(resume_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ResumeResponse:
    r = await db.get(Resume, resume_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    return _to_response(r)


@router.post("", response_model=ResumeResponse, status_code=201)
async def upload_resume(
    file: UploadFile = File(...),
    tags: str | None = Form(default=None),
    is_default: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
) -> ResumeResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(raw) > MAX_RESUME_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Resume exceeds {MAX_RESUME_BYTES // (1024 * 1024)} MB limit",
        )

    # Pick a safe storage filename: UUID + extension derived from the user filename.
    original_name = (file.filename or "resume").strip()
    ext = Path(original_name).suffix.lower()
    storage_filename = f"{uuid.uuid4().hex}{ext}"
    on_disk = _resume_disk_path(storage_filename)
    on_disk.write_bytes(raw)  # sync write; small files + bounded count.

    content_type = file.content_type or _EXT_TO_CT.get(ext, "application/octet-stream")
    if content_type not in ALLOWED_CONTENT_TYPES:
        try:
            on_disk.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {content_type}",
        )

    try:
        new_resume = Resume(
            name=original_name[:512],
            storage_path=storage_filename,
            content_type=content_type,
            size_bytes=len(raw),
            tags=_normalize_tags(tags, None),
            is_default=is_default,
            extracted_text=extract_text(on_disk, content_type) or None,
        )
        if is_default:
            # Demote any existing default in the same transaction.
            await db.execute(
                Resume.__table__.update().where(Resume.is_default.is_(True)).values(is_default=False)
            )
        db.add(new_resume)
        await db.commit()
        await db.refresh(new_resume)
    except Exception:
        # Remove orphaned file on DB failure.
        try:
            on_disk.unlink(missing_ok=True)
        except OSError:
            pass
        await db.rollback()
        raise

    logger.info(
        "Resume uploaded: id=%s name=%s size=%d is_default=%s tags=%s",
        new_resume.id, new_resume.name, new_resume.size_bytes,
        new_resume.is_default, new_resume.tags,
    )
    return _to_response(new_resume)


@router.patch("/{resume_id}", response_model=ResumeResponse)
async def update_resume(
    resume_id: uuid.UUID,
    payload: ResumeUpdate,
    db: AsyncSession = Depends(get_db),
) -> ResumeResponse:
    r = await db.get(Resume, resume_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")

    if payload.tags is not None:
        r.tags = _normalize_tags(",".join(payload.tags), None)
    if payload.is_default is not None and payload.is_default and not r.is_default:
        await db.execute(
            Resume.__table__.update().where(Resume.is_default.is_(True)).values(is_default=False)
        )
        r.is_default = True
    elif payload.is_default is not None:
        r.is_default = payload.is_default

    await db.commit()
    await db.refresh(r)
    return _to_response(r)


@router.delete("/{resume_id}", status_code=204)
async def delete_resume(resume_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    r = await db.get(Resume, resume_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    on_disk = _resume_disk_path(r.storage_path)
    await db.delete(r)
    await db.commit()
    try:
        on_disk.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Failed to remove resume file {on_disk}: {e}")


@router.get("/{resume_id}/download")
async def download_resume(resume_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    r = await db.get(Resume, resume_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    path = _resume_disk_path(r.storage_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="Resume file missing on disk")
    # `attachment` forces the browser to download — prevents in-origin PDF preview XSS.
    return FileResponse(
        path=str(path),
        media_type=r.content_type,
        filename=r.name,
        headers={"Content-Disposition": f'attachment; filename="{r.name}"'},
    )
