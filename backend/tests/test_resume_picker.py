"""Tests for ``apply_worker.resume_picker.pick_resume``.

Covers:

* ``derive_role_family_tags`` — pure substring matcher; tested as
  a fixed map so the test surface is reproducible across LLM
  version bumps.
* Tag-match winner — production-AI / ML / Forward-deployed cues
  map cleanly onto the operator's tagged resumes.
* Default-only fallback — when no resume has a matching tag but
  one is ``is_default=True``, the picker still returns it (no
  LLM call).
* Tiebreaker — two equally-scored resumes; the newer ``uploaded_at``
  wins.
* LLM fallback — only fires when (a) no tag matched AND (b) no
  default resume exists. The AsyncMock returns a chosen id from
  the candidate pool.
* LLM refusal — the picker returns ``None`` when the LLM
  declines (``resume_id is None``).
* LLM error — graceful ``None`` return when the AsyncMock raises.

The ``llm_client`` parameter is always an ``AsyncMock`` so tests
are LLM-free. ``pick_best_resume`` is awaited via ``asyncio.run``
because each test only awaits once — pytest-asyncio's
``asyncio_mode = "auto"`` (set in pyproject.toml) would handle it
too but ``asyncio.run`` keeps the test bodies readable for
contributors who haven't seen pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from apply_worker.resume_picker import (
    DEFAULT_BONUS,
    derive_role_family_tags,
    pick_resume,
)
from apply_worker.types import ResumeRecord


def _run(coro):
    """Tiny ``asyncio.run`` shim so each test reads as one await."""
    return asyncio.run(coro)


# --------------------------------------------------------------------
# ``derive_role_family_tags`` — pure substring matcher
# --------------------------------------------------------------------


def test_derive_role_family_tags_empty_input_returns_empty_set():
    assert derive_role_family_tags(title="", description="") == set()


def test_derive_role_family_tags_production_ai_cue():
    """``ai engineer`` + ``inference`` cues both flag ``production-ai``."""
    tags = derive_role_family_tags(
        title="Senior AI Engineer",
        description="Owns inference platform reliability",
    )
    assert tags == {"production-ai"}


def test_derive_role_family_tags_multiple_families():
    """A JD that mixes platform + safety flags both families."""
    tags = derive_role_family_tags(
        title="ML Security Engineer",
        description="Red team + adversarial robustness for the model serving stack",
    )
    assert "ai-security" in tags
    assert "production-ai" in tags


def test_derive_role_family_tags_forward_deployed_cue():
    tags = derive_role_family_tags(
        title="Customer Engineer",
        description="Forward deployed at top accounts",
    )
    assert tags == {"forward-deployed"}


def test_derive_role_family_tags_no_match_returns_empty():
    tags = derive_role_family_tags(
        title="Backend Engineer",
        description="Go services, Postgres, gRPC",
    )
    assert tags == set()


def test_derive_role_family_tags_custom_overrides_default():
    """An operator can onboard a new family without a code change."""
    tags = derive_role_family_tags(
        title="Robotics Engineer",
        description="ROS 2 + motion planning",
        family_keywords={"robotics": ["robotics", "ros 2", "motion planning"]},
    )
    assert tags == {"robotics"}
    # Default map still doesn't fire because custom override wipes it.
    assert derive_role_family_tags(
        title="Robotics Engineer",
        description="ROS 2 + motion planning",
        family_keywords={"robotics": ["robotics"]},
    ) == {"robotics"}


# --------------------------------------------------------------------
# Tag-match winner
# --------------------------------------------------------------------


def test_pick_resume_tag_match_wins_over_default():
    """``production-ai``-tagged resume beats an untagged default.

    With ``DEFAULT_BONUS=0.5``, the default scores ``0.5`` (bonus
    only — no tag overlap) and the production-AI resume scores
    ``1.0`` (one tag overlap). The matched resume wins.
    """
    default_resume = ResumeRecord(
        id="r-default",
        name="default.pdf",
        tags=[],
        is_default=True,
        uploaded_at="2024-01-01T00:00:00Z",
    )
    production_ai_resume = ResumeRecord(
        id="r-prod-ai",
        name="prod_ai.pdf",
        tags=["production-ai"],
        is_default=False,
        uploaded_at="2024-06-01T00:00:00Z",
    )
    chosen = _run(
        pick_resume(
            job={"title": "Senior AI Engineer", "description": "inference platform"},
            resumes=[default_resume, production_ai_resume],
        )
    )
    assert chosen is not None
    assert chosen.id == "r-prod-ai"
    assert chosen.is_default is False


def test_pick_resume_no_tag_match_default_wins_via_bonus():
    """No ``family_tags`` fires; the curated default wins via +0.5 bonus."""
    default_resume = ResumeRecord(
        id="r-default",
        name="default.pdf",
        tags=[],
        is_default=True,
    )
    other_resume = ResumeRecord(
        id="r-other",
        name="other.pdf",
        tags=["forward-deployed"],
        is_default=False,
    )
    chosen = _run(
        pick_resume(
            job={"title": "Backend Engineer", "description": "Go services"},  # no cues
            resumes=[default_resume, other_resume],
        )
    )
    # The default scores 0.5 (bonus); the other resume has tags but
    # tags don't fire on the job's text so its overlap is 0.
    assert chosen is not None
    assert chosen.id == "r-default"


# --------------------------------------------------------------------
# Tiebreaker: newest upload wins when scores tie
# --------------------------------------------------------------------


def test_pick_resume_tiebreak_newest_upload_wins():
    """Two equally-scoring resumes; the newer ``uploaded_at`` wins."""
    older = ResumeRecord(
        id="r-old",
        name="ai_old.pdf",
        tags=["production-ai"],
        is_default=False,
        uploaded_at="2024-01-01T00:00:00Z",
    )
    newer = ResumeRecord(
        id="r-new",
        name="ai_new.pdf",
        tags=["production-ai"],
        is_default=False,
        uploaded_at="2024-12-31T00:00:00Z",
    )
    chosen = _run(
        pick_resume(
            job={"title": "AI Engineer"},
            resumes=[older, newer],
        )
    )
    assert chosen.id == "r-new"


# --------------------------------------------------------------------
# LLM fallback paths
# --------------------------------------------------------------------


def test_pick_resume_llm_fallback_only_when_tag_match_silent_and_no_default():
    """No tag match + no default + LLM picks one → LLM-fallback winner."""
    resumes = [
        ResumeRecord(id="r-a", name="a.pdf", tags=["forward-deployed"]),
        ResumeRecord(id="r-b", name="b.pdf", tags=["production-ai"]),
    ]
    # Both resumes have tags but ``family_tags`` is empty (no cues
    # fire on this job), so neither scores > 0 unless the default
    # bonus kicks in.
    llm = AsyncMock()
    llm.pick_best_resume.return_value = ("r-b", 0.85)
    chosen = _run(
        pick_resume(
            job={"title": "Backend Engineer", "description": "Go"},  # no cues
            resumes=resumes,
            llm_client=llm,
        )
    )
    assert chosen is not None
    assert chosen.id == "r-b"
    llm.pick_best_resume.assert_awaited_once()


def test_pick_resume_llm_refusal_returns_none():
    """LLM returns ``(None, confidence)``; picker surfaces ``None``."""
    resumes = [
        ResumeRecord(id="r-a", name="a.pdf", tags=["forward-deployed"]),
    ]
    llm = AsyncMock()
    llm.pick_best_resume.return_value = (None, 0.0)
    chosen = _run(
        pick_resume(
            job={"title": "Backend Engineer"},
            resumes=resumes,
            llm_client=llm,
        )
    )
    assert chosen is None


def test_pick_resume_llm_hallucinated_id_returns_none():
    """LLM returns an id that isn't in the candidate pool → ``None``."""
    resumes = [
        ResumeRecord(id="r-a", name="a.pdf", tags=["forward-deployed"]),
    ]
    llm = AsyncMock()
    llm.pick_best_resume.return_value = ("r-XX-XX", 0.85)
    chosen = _run(
        pick_resume(
            job={"title": "Backend Engineer"},
            resumes=resumes,
            llm_client=llm,
        )
    )
    assert chosen is None


def test_pick_resume_llm_raises_returns_none():
    """LLM raises (404, timeout, etc.); picker surfaces ``None``.

    The picker never lets an LLM error abort the apply flow —
    instead the worker surfaces the ``None`` outcome to the
    operator as "manually pick a resume, then re-queue".
    """
    resumes = [
        ResumeRecord(id="r-a", name="a.pdf", tags=["forward-deployed"]),
    ]
    llm = AsyncMock()
    llm.pick_best_resume.side_effect = RuntimeError("upstream 404")
    chosen = _run(
        pick_resume(
            job={"title": "Backend Engineer"},
            resumes=resumes,
            llm_client=llm,
        )
    )
    assert chosen is None


# --------------------------------------------------------------------
# Empty / missing inputs
# --------------------------------------------------------------------


def test_pick_resume_no_resumes_returns_none():
    chosen = _run(pick_resume(job={"title": "AI Engineer"}, resumes=None))
    assert chosen is None


def test_pick_resume_empty_resume_list_returns_none():
    chosen = _run(pick_resume(job={"title": "AI Engineer"}, resumes=[]))
    assert chosen is None


def test_pick_resume_plain_dicts_work():
    """The picker accepts plain ``dict`` for both ``job`` and ``resumes``.

    Mirrors the production shape: ``Job.model_dump(mode="json")``
    + ``Resume.model_dump(mode="json")`` — no field-by-field
    conversion required.
    """
    chosen = _run(
        pick_resume(
            job={"title": "AI Engineer"},
            resumes=[
                {"id": "r-default", "name": "default.pdf", "tags": [], "is_default": True},
                {"id": "r-prod", "name": "prod.pdf", "tags": ["production-ai"]},
            ],
        )
    )
    assert chosen is not None
    assert chosen.id == "r-prod"


# --------------------------------------------------------------------
# Bonus-weight configurability
# --------------------------------------------------------------------


def test_pick_resume_default_bonus_zero_disables_default_boost():
    """When ``default_bonus=0``, the default no longer outranks an empty overlap."""
    default_resume = ResumeRecord(
        id="r-default",
        name="default.pdf",
        tags=[],
        is_default=True,
    )
    other_resume = ResumeRecord(
        id="r-other",
        name="other.pdf",
        tags=["production-ai"],
    )
    chosen = _run(
        pick_resume(
            job={"title": "AI Engineer"},
            resumes=[default_resume, other_resume],
            default_bonus=0.0,
        )
    )
    # other has tag overlap (production-ai) → score 1.0
    # default has 0 tags and bonus=0 → score 0.0
    assert chosen is not None
    assert chosen.id == "r-other"


def test_pick_resume_default_bonus_constant_is_half():
    """Pin against an accidental bonus spike — DEFAULT_BONUS is a tunable knob."""
    assert DEFAULT_BONUS == 0.5
