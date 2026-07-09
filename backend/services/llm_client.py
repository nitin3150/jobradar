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
* ``NVIDIA_RPM`` — defaults to :data:`DEFAULT_NVIDIA_RPM` (40). Sets the
  per-process rate-limit on NVIDIA calls so the GHA boards-scan workflow
  doesn't trip 429. ``0`` disables the limiter.
* ``GROQ_API_KEY`` — required to enable Groq as a provider.
* ``GROQ_BASE_URL`` — defaults to ``https://api.groq.com/openai/v1``.
* ``GROQ_MODEL`` — defaults to ``llama-3.3-70b-versatile``.

At least one provider must be configured. :class:`ProviderConfig` is exported
so tests can inject fake providers without touching the process environment.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
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

# Default NVIDIA requests-per-minute budget. The free / dev-tier NVIDIA
# NIM API key is throttled at 40 RPM; we respect that by default so
# opportunistic bulk scans (e.g. the hourly boards-scan GHA workflow)
# never trip a 429. Operators with a higher-tier key can raise the
# ceiling via the ``NVIDIA_RPM`` env var; ``NVIDIA_RPM=0`` disables
# the limiter entirely (each request fires immediately).
DEFAULT_NVIDIA_RPM: Final = 40

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


class AsyncTokenBucket:
    """Async token-bucket rate limiter for LLM API quotas.

    Capacity is the maximum burst size; ``refill_per_second`` is the
    long-run average rate. The bucket starts full so the first
    ``capacity`` calls fire immediately. Subsequent calls block
    (asynchronously — they ``await asyncio.sleep`` rather than spin)
    until enough virtual time has passed for another token to refill.

    The bucket is process-local: two ``AsyncTokenBucket`` instances
    track independent budgets. The module-level
    :data:`_NVIDIA_RPM_LIMITERS` cache is the recommended way to
    share a single bucket across ``LLMClient`` instances within a
    process — see :func:`_nvidia_rate_limiter_from_env` for the
    reason (per-call buckets would let a long-running FastAPI process
    collectively exceed the API key's per-minute budget).
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        if capacity < 1:
            raise ValueError(
                f"capacity must be >= 1 (got {capacity}); a non-positive "
                f"burst budget would make acquire() block forever."
            )
        if refill_per_second <= 0:
            raise ValueError(
                f"refill_per_second must be > 0 (got {refill_per_second}); "
                f"a non-positive refill rate would make acquire() block forever."
            )
        self._capacity = float(capacity)
        self._refill = float(refill_per_second)
        # Start full so the operator's first ``capacity`` calls (the
        # common case for an interactive run) don't pay a startup delay.
        self._tokens = float(capacity)
        self._last = time.monotonic()
        # Single asyncio.Lock serialises all acquires; the cost is
        # negligible compared to the LLM call we're protecting.
        self._lock = asyncio.Lock()

    @property
    def available_tokens(self) -> float:
        """Snapshot the current token count — read-only, no refill applied.

        Used by tests to assert the bucket's steady-state behaviour
        without having to instrument ``acquire()`` internals.
        """
        return self._tokens

    async def acquire(self) -> None:
        """Block until 1 token is available, then consume it.

        Implements the token-bucket algorithm. On every call we
        credit the bucket with ``elapsed * refill`` tokens (capped at
        ``capacity``), then either consume a token (returning
        immediately) or sleep just long enough to accrue 1 token
        before retrying.

        ``asyncio.CancelledError`` propagating through ``asyncio.sleep``
        releases the lock via ``async with`` cleanup and does NOT
        consume a token (the consumer never reached the decrement
        line). This is the right behaviour for a worker that gets
        cancelled mid-wait — the budget is preserved for the next
        call.
        """
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                # Cap at capacity so a long idle period doesn't
                # accumulate an unbounded "credit balance" that would
                # cause a subsequent burst to blow through the limit.
                self._tokens = min(
                    self._capacity, self._tokens + elapsed * self._refill
                )
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Bucket doesn't have a token yet — sleep outside the
                # lock for the exact deficit / refill-rate duration.
                deficit = 1.0 - self._tokens
                wait_seconds = deficit / self._refill
            await asyncio.sleep(wait_seconds)


class _PermanentError(Exception):
    """Won't help to retry or fall back: bad input, auth, or unparseable response."""


# Module-level rate-limiter cache so the 40-RPM budget is process-global,
# not per-LLMClient. Two concurrent ``LLMClient.from_env()`` calls in a
# long-running FastAPI process otherwise each construct a fresh bucket
# and the process collectively uses 2 × NVIDIA_RPM, which would still
# trip the upstream 429 the limiter is meant to prevent.
#
# Keyed by the integer RPM so callers that raise the limit (e.g. a
# higher-tier key with NVIDIA_RPM=200) get a separate bucket from a
# caller that leaves the default 40 — the two operate on independent
# budgets, which is what the operator intends when they set the env var.
_NVIDIA_RPM_LIMITERS: dict[int, AsyncTokenBucket] = {}


def _nvidia_rate_limiter_from_env() -> AsyncTokenBucket | None:
    """Read ``NVIDIA_RPM`` and build (or fetch) the matching token bucket.

    Returns ``None`` when ``NVIDIA_RPM`` is set to ``0`` or any
    non-positive value — that disables the limiter. A malformed
    value (``NVIDIA_RPM=abc``) raises ``ValueError`` so the operator
    sees the misconfig at boot, not at the first scoring call.

    We log the chosen RPM at INFO so the operator can confirm the
    limiter is active without having to read code. The bucket is
    created with ``capacity == rpm`` and ``refill_per_second ==
    rpm / 60`` so a fully-drained bucket recovers to its burst
    capacity in exactly 60 seconds.
    """
    raw = os.environ.get("NVIDIA_RPM", str(DEFAULT_NVIDIA_RPM)).strip()
    try:
        rpm = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"NVIDIA_RPM={raw!r} is not a valid integer (expected a "
            f"non-negative request-per-minute budget, e.g. '40', '0')."
        ) from exc
    if rpm <= 0:
        return None
    bucket = _NVIDIA_RPM_LIMITERS.get(rpm)
    if bucket is None:
        bucket = AsyncTokenBucket(capacity=rpm, refill_per_second=rpm / 60.0)
        _NVIDIA_RPM_LIMITERS[rpm] = bucket
        logging.getLogger("jobradar.llm").info(
            "NVIDIA rate limiter active: %d RPM (capacity=%d, refill=%.4f/s). "
            "Override with NVIDIA_RPM=0 to disable. Cached at module level "
            "so the budget is process-global, not per-LLMClient.",
            rpm,
            rpm,
            rpm / 60.0,
        )
    return bucket


class ProviderConfig(NamedTuple):
    """One slot in the LLM provider chain.

    ``rate_limiter`` is an optional :class:`AsyncTokenBucket`; when set,
    :meth:`LLMClient.score_opportunity` will ``await acquire()`` before
    the first attempt at this provider. We throttle the *primary* so
    bulk scans never trip the upstream 429; the fallback can also be
    throttled in the same way if a future operator hits Groq's free
    tier ceiling, but JobRadar's v1 only throttles NVIDIA.
    """

    name: str
    base_url: str
    api_key: str
    model: str
    rate_limiter: AsyncTokenBucket | None = None


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

        NVIDIA is throttled by an :class:`AsyncTokenBucket` whose
        capacity and refill rate are read from the ``NVIDIA_RPM`` env
        var (default :data:`DEFAULT_NVIDIA_RPM`, i.e. 40 RPM). Set
        ``NVIDIA_RPM=0`` to disable the limiter — useful for higher
        tier keys, integration tests, and one-off bulk rescans.
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
                    rate_limiter=_nvidia_rate_limiter_from_env(),
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

        If a provider has a :attr:`ProviderConfig.rate_limiter` set, we
        ``await acquire()`` once per opportunity (outside the retry
        loop). That means a transient-failure retry does **not**
        consume a second token — the operator paid for the attempt
        and the retry is a recovery, not a fresh budget hit. If the
        retry also fails, we advance to the next provider (and acquire
        *that* provider's token independently).
        """
        last_exc: Exception | None = None
        for provider in self.providers:
            if provider.rate_limiter is not None:
                # One token per opportunity, regardless of how many
                # internal retries happen on this provider. This keeps
                # bulk scans at-or-under the configured RPM even when
                # the upstream has transient errors.
                await provider.rate_limiter.acquire()
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
    "DEFAULT_NVIDIA_RPM",
    "SYSTEM_PROMPT",
    "AsyncTokenBucket",
    "ProviderConfig",
    "LLMClient",
    "build_prompt",
    "parse_score_response",
]
