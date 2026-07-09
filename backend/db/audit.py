"""Audit-trail helpers — the shared insert path for any table that
records "what just changed, and why" rows.

The JobRadar ``job_status_history`` table is the canonical example:
every status transition of a :class:`db.models.Job` row writes a
``JobStatusHistory`` row in the *same* transaction so a future
audit query can never observe a status with no history. Multiple
routers need to do this (currently :mod:`routes.jobs` and
:mod:`routes.applications`; future routers will follow), so the
insert helper lives here in the ``db`` package rather than
inlined into one router — that way a new router that needs to
write a transition does ``from db.audit import record_status_history``
as a clean top-level import instead of copy-pasting the inline
``session.add(...)`` block.

Why ``db/audit.py`` (not ``routes/_audit.py``):
- The function writes to a DB table; it is a DB-layer concern,
  not a route concern.
- The function depends only on ``db.models`` and the
  ``AsyncSession`` type — no FastAPI imports.
- Any future router (outreach, auto_apply, etc.) that needs to
  write a status transition imports from here without crossing
  the ``routes/`` package boundary.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from db import models as db_models


def record_status_history(
    session: AsyncSession,
    job_id: UUID,
    from_status: str | None,
    to_status: str,
    source: str,
    note: str | None,
) -> db_models.JobStatusHistory:
    """Append a ``job_status_history`` row in the *current* session.

    The caller is responsible for ``session.commit()`` so the
    history row and the parent ``jobs.status`` update land in the
    same transaction. Splitting them would let a future observer
    see a status change with no history — which is exactly the
    bug the v0.5 audit-trail rebuild is meant to prevent.

    ``source`` defaults to ``JOB_STATUS_SOURCE_USER`` (``"user"``)
    when the caller passes an empty string or ``None``; the
    canonical source values live on :mod:`db.models` and
    expanding the set is a one-line edit to the enum-like
    constants there.
    """
    history = db_models.JobStatusHistory(
        job_id=job_id,
        from_status=from_status,
        to_status=to_status,
        source=source or db_models.JOB_STATUS_SOURCE_USER,
        note=note,
    )
    session.add(history)
    return history


__all__ = ["record_status_history"]
