"""Settings router — singleton user preferences for the React ``PreferencesModal``.

The frontend calls :func:`fetchPreferences` and :func:`updatePreferences`
from ``frontend/src/api/client.js`` against ``/api/settings``. The hook
fallback in :mod:`frontend/src/hooks/usePreferences` exports a
``DEFAULT_PREFERENCES`` constant that we mirror here exactly so the
shape stays consistent during the initial paint before the GET resolves.

Wire shape (single object — the hook does NOT wrap it in an envelope):

* ``target_roles: list[str]`` — comma-separated values from the
  PreferencesModal ``EditableList`` textbox; server ``PATCH`` normalizes
  (trim, drop blanks, dedupe while preserving order) so React Query's
  ``setQueryData`` post-write reflects server-side cleanup.
* ``review_window_hours: float`` — how long the user has to approve a
  job before the deadline action fires. Bounded ``[0.5, 48]``.
* ``job_fit_threshold: float`` — minimum AI fit score (``0.0`` to
  ``1.0``) the LLM ranker requires before the job enters the review
  queue. Below this, the job is dropped before the user sees it.
* ``send_followup_emails: bool`` — toggle for the 5-day courtesy
  follow-up via the Gmail connector (out of scope here).

Storage is an in-process dict — preferences do not survive process
restarts. Swap for the real DB-backed store when the persistence layer
lands.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field


router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class Preferences(BaseModel):
    target_roles: list[str] = Field(
        default_factory=lambda: [
            "AI Engineer",
            "Machine Learning Engineer",
            "LLM Engineer",
            "Software Engineer",
        ],
        description="Match keywords used during discovery + job prefilter.",
    )
    review_window_hours: float = Field(
        default=2.0,
        ge=0.5,
        le=48.0,
        description="Hours to approve before the deadline action runs.",
    )
    job_fit_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Minimum AI fit score (0.0-1.0).",
    )
    send_followup_emails: bool = Field(
        default=True,
        description="Send a polite follow-up 5 days after applying if no reply.",
    )


class PreferencesPatch(BaseModel):
    target_roles: Optional[list[str]] = None
    review_window_hours: Optional[float] = Field(default=None, ge=0.5, le=48.0)
    job_fit_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    send_followup_emails: Optional[bool] = None


# ---------------------------------------------------------------------------
# Singleton storage — in-process only. The PATCH handler normalizes
# ``target_roles`` aggressively so the React ``setQueryData`` cache
# reconciliation in ``usePreferences`` reflects server-side cleanup.
# ---------------------------------------------------------------------------
_PREFS_STATE: dict = {
    "data": Preferences().model_dump(),
    "updated_at": "2026-01-10T00:00:00Z",
}


def _reset_prefs() -> None:
    """Reset preferences to factory defaults — test seam mirroring
    :func:`routes.pipeline._reset_state`.

    Production code never calls this; lifecycle is the PATCH handler.
    Tests drive this from ``setUp`` so background PATCHes from a
    previous case don't leak into the next.
    """
    _PREFS_STATE["data"] = Preferences().model_dump()
    _PREFS_STATE["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_roles(raw: list[str]) -> list[str]:
    """Trim / drop blanks / dedupe while preserving first-occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        s = (v or "").strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Routes — the singleton has no path segments, just ``/``. Order does
# not matter for these two declarations but we keep GET before PATCH to
# match the read-then-write mental model used in the React hook.
# ---------------------------------------------------------------------------
@router.get("", response_model=Preferences)
def get_preferences() -> Preferences:
    return Preferences(**_PREFS_STATE["data"])


@router.patch("", response_model=Preferences)
def patch_preferences(payload: PreferencesPatch) -> Preferences:
    data = dict(_PREFS_STATE["data"])

    if payload.target_roles is not None:
        data["target_roles"] = _normalize_roles(payload.target_roles)

    if payload.review_window_hours is not None:
        data["review_window_hours"] = payload.review_window_hours

    if payload.job_fit_threshold is not None:
        data["job_fit_threshold"] = payload.job_fit_threshold

    if payload.send_followup_emails is not None:
        data["send_followup_emails"] = payload.send_followup_emails

    _PREFS_STATE["data"] = data
    _PREFS_STATE["updated_at"] = _now_iso()
    return Preferences(**data)
