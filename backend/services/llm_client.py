"""OpenAI-compatible LLM client with NVIDIA-primary + Groq-fallback retry chain.

Why this exists
===============

The user wants every scanned opportunity scored against their profile by an
LLM. The two endpoints they have credentials for are:

* **NVIDIA NIM** — hosted catalogue of OSS instruction-tuned models, OpenAI
  API-compatible at ``https://integrate.api.nvidia.com/v1``. The default
  model is ``meta/llama-3.1-70b-instruct`` — strong at classification tasks
  and well-suited to a single-token ``score (0.0-1.0) + reasoning`` extraction.
* **Groq** — fast LPU inference for OSS models, OpenAI API-compatible at
  ``https://api.groq.com/openai/v1``. Fallback is ``llama-3.3-70b-versatile``.

Both endpoints accept the standard OpenAI Python SDK request shape when you
override ``base_url`` at client construction time. This module wraps the
official ``openai`` SDK with retry-on-transient + advance-to-next-provider
semantics so a flaky primary doesn't double the per-opportunity cost on
average.

Configuration
=============

The defaults read from the process environment on each
:meth:`LLMClient.from_env` call:

* ``NVIDIA_API_KEY`` — required to enable NVIDIA as a provider.
* ``NVIDIA_BASE_URL`` — defaults to ``https://integrate.api.nvidia.com/v1``.
* ``NVIDIA_MODEL`` — defaults to ``meta/llama-3.1-70b-instruct``.
* ``GROQ_API_KEY`` — required to enable Groq as a provider.
* ``GROQ_BASE_URL`` — defaults to ``https://api.groq.com/openai/v1``.
* ``GROQ_MODEL`` — defaults to ``llama-3.3-70b-versatile``.

At least one provider must be configured. :class:`ProviderConfig` is exported
so tests can inject fake providers without touching the process environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Final, NamedTuple

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

DEFAULT_NVIDIA_BASE: Final = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL: Final = "meta/llama-3.1-70b-instruct"
DEFAULT_GROQ_BASE: Final = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL: Final = "llama-3.3-70b-versatile"

# System prompt applied to every opportunity so score calibration stays
# consistent across providers.
SYSTEM_PROMPT: Final = (
    "You are a job-fit scoring assistant. Given a candidate's profile and "
    "a single job/opportunity, output strict JSON with this shape:\n\n"
    "{\"score\": <float 0.0-1.0>, \"reasoning\": \"<one or two sentences>\"}\n\n"
    "The score reflects role-fit and seniority alignment: 0.0 = clearly "
    "off-profile, 1.0 = exact match. Higher means stronger fit. Return "
    "ONLY the JSON object, with no preamble, no markdown."
)

# JSON object extractor — fallback when the model hands back prose around
# the JSON. Captures the first ``{...}`` mentioning both ``score`` and
# ``reasoning`` keys.
_JSON_OBJ_RE = re.compile(
    r"\{[^{}]*\"score\"[^{}]*\"reasoning\"[^{}]*\}",
    re.DOTALL,
)


class _PermanentError(Exception):
    """Won't help to retry or fall back: bad input, auth, or unparseable response."""


class ProviderConfig(NamedTuple):
    """One slot in the LLM provider chain."""

    name: str
    base_url: str
    api_key: str
    model: str


class LLMClient:
    """Scoring client with retry-then-fallback across providers.

    Providers are tried in order. Within a provider, transient failures
    (timeout, 5xx, connection, rate-limit) trigger one retry before we
    advance to the next provider. ``BadRequest`` / ``Authentication`` /
    ``NotFound`` are treated as permanent — they won't help if we just
    try again, so we advance immediately.

    Cost ceiling: a single opportunity costs at most ``len(providers) * 2``
    LLM calls (1 attempt + 1 retry per provider) before :meth:`score_opportunity`
    raises :class:`RuntimeError`.
    """

    def __init__(self, providers: list[ProviderConfig]) -> None:
        if not providers:
            raise ValueError("providers list cannot be empty")
        self.providers = providers
        # ``AsyncOpenAI`` is async-only; we instantiate one per provider because
        # the underlying SDK holds an ``httpx.AsyncClient`` bound to the asyncio
        # loop at construction time. Re-using across loops causes warnings — we
        # avoid the footgun by keeping the count small (≤ 2 providers in v1).
        self._clients: dict[str, AsyncOpenAI] = {
            p.name: AsyncOpenAI(api_key=p.api_key, base_url=p.base_url)
            for p in providers
        }

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Construct an :class:`LLMClient` from process env vars.

        Providers are included only if their API key is set. Order is
        NVIDIA first, Groq second so the cheaper/quicker primary is
        tried first by default.
        """
        providers: list[ProviderConfig] = []
        nvidia_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if nvidia_key:
            providers.append(
                ProviderConfig(
                    name="nvidia",
                    base_url=os.environ.get("NVIDIA_BASE_URL", DEFAULT_NVIDIA_BASE),
                    api_key=nvidia_key,
                    model=os.environ.get("NVIDIA_MODEL", DEFAULT_NVIDIA_MODEL),
                )
            )
        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        if groq_key:
            providers.append(
                ProviderConfig(
                    name="groq",
                    base_url=os.environ.get("GROQ_BASE_URL", DEFAULT_GROQ_BASE),
                    api_key=groq_key,
                    model=os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL),
                )
            )
        if not providers:
            raise RuntimeError(
                "no LLM provider configured — set NVIDIA_API_KEY and/or "
                "GROQ_API_KEY in the environment (see backend/.env.example)"
            )
        return cls(providers)

    async def score_opportunity(
        self, profile_summary: str, opportunity: dict
    ) -> tuple[float, str]:
        """Score one opportunity against the candidate profile.

        Returns ``(score, reasoning)`` where ``score`` is clamped to
        ``[0.0, 1.0]`` and ``reasoning`` is at most 400 chars. Raises
        :class:`RuntimeError` if every provider in the chain fails.
        """
        last_exc: Exception | None = None
        for provider in self.providers:
            client = self._clients[provider.name]
            for attempt in (1, 2):
                try:
                    return await self._score_once(
                        client, provider.model, profile_summary, opportunity
                    )
                except _PermanentError as exc:
                    last_exc = exc
                    break  # unrecoverable on this provider; advance
                except Exception as exc:  # noqa: BLE001 — transient
                    last_exc = exc
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                    # else: fall through to next provider
        # Avoid ``str(last_exc)`` because the openai SDK's exception
        # ``__str__`` can probe ``self.body`` for JSON-parsing; mocking
        # ``body=None`` with a MagicMock response makes that probe raise
        # a confusing ``TypeError`` rather than surface the real cause.
        # Chained ``from last_exc`` preserves the full traceback in logs.
        raise RuntimeError(
            f"all LLM providers failed; last error type={type(last_exc).__name__}"
        ) from last_exc

    async def _score_once(
        self,
        client: AsyncOpenAI,
        model: str,
        profile_summary: str,
        opportunity: dict,
    ) -> tuple[float, str]:
        prompt = build_prompt(profile_summary, opportunity)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=300,
            )
        except (AuthenticationError, BadRequestError, NotFoundError) as exc:
            raise _PermanentError(f"{type(exc).__name__}: {exc}") from exc
        except (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        ):
            raise  # transient; caller decides retry vs advance
        content = (resp.choices[0].message.content or "").strip()
        return parse_score_response(content)


def build_prompt(profile_summary: str, opportunity: dict) -> str:
    """Render the user-side prompt: profile + salient opportunity fields."""
    selected_keys = (
        "title",
        "company_name",
        "url",
        "description",
        "source",
        "category",
        "ats_type",
    )
    opp_lines: list[str] = []
    for key in selected_keys:
        val = opportunity.get(key)
        if val:
            opp_lines.append(f"{key}: {val}")
    opp_text = "\n".join(opp_lines) or json.dumps(opportunity, indent=2, default=str)[:1500]
    return (
        "Candidate profile:\n"
        f"{profile_summary.strip()}\n\n"
        "Opportunity:\n"
        f"{opp_text}\n\n"
        "Return strict JSON with score (0.0-1.0) and reasoning."
    )


def parse_score_response(content: str) -> tuple[float, str]:
    """Parse the JSON response, with a regex fallback if the model wraps it in prose."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`\n ")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return _coerce_score(json.loads(text))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    match = _JSON_OBJ_RE.search(content)
    if match:
        try:
            return _coerce_score(json.loads(match.group(0)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    raise _PermanentError(f"could not parse LLM response: {content[:200]!r}")


def _coerce_score(data: dict) -> tuple[float, str]:
    score = float(data["score"])
    score = max(0.0, min(1.0, score))
    reasoning = str(data["reasoning"])[:400]
    return score, reasoning


__all__ = [
    "DEFAULT_NVIDIA_BASE",
    "DEFAULT_NVIDIA_MODEL",
    "DEFAULT_GROQ_BASE",
    "DEFAULT_GROQ_MODEL",
    "SYSTEM_PROMPT",
    "ProviderConfig",
    "LLMClient",
    "build_prompt",
    "parse_score_response",
]
