"""Best-effort resume text extraction. Never raises — failures return ""."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_EXTRACT_CHARS = 20_000

_DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TEXT_CTS = {"text/plain", "text/markdown"}


def extract_text(path: Path, content_type: str) -> str:
    """Extract plain text from a resume file. Returns "" on any failure or
    unsupported type (legacy .doc / application/msword is not supported)."""
    try:
        if content_type == "application/pdf":
            text = _extract_pdf(path)
        elif content_type == _DOCX_CT:
            text = _extract_docx(path)
        elif content_type in _TEXT_CTS:
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            return ""
        return text[:MAX_EXTRACT_CHARS]
    except Exception as e:
        logger.warning("Resume text extraction failed for %s: %s", path, e)
        return ""


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)
