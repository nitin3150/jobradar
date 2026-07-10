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
#
# Profile-aware: instructs the LLM to use the FULL profile context
# (target roles with fit levels, narrative, compensation, location,
# superpowers, proof points) when scoring — not just the role titles.
# The user-side prompt (:func:`build_prompt`) already passes the
# rendered :func:`services.profile_service.build_profile_summary`
# output verbatim, so every section the renderer emits (Target roles,
# Headline, Exit story, Superpowers, Proof points, Candidate, Target
# comp, Location) is available to the model. The system prompt's job
# is to tell the model to ACTUALLY USE all of it instead of anchoring
# on the role titles alone.
#
# The scoring-factor list mirrors the sections :func:`build_profile_summary`
# renders, in the same order — so a reader can mentally map "the
# system prompt told me to check seniority alignment" to "the
# profile summary has archetype.level right there". This is the same
# context-parity trick the resume extractor uses in
# :data:`PROFILE_EXTRACTION_SYSTEM_PROMPT`.
SYSTEM_PROMPT: Final = (
    "You are a job-fit scoring assistant. Given a candidate's complete "
    "profile (target roles with fit levels, narrative, compensation, "
    "location) and a single job/opportunity, output strict JSON with "
    "this shape:\n\n"
    "{\"score\": <float 0.0-1.0>, \"reasoning\": \"<one or two sentences>\"}\n\n"
    "SCORING FACTORS — consider ALL of the following when calibrating "
    "the score:\n\n"
    "1. ROLE FIT: Does the opportunity's role family match the candidate's "
    "target_roles? Primary roles are dream matches (0.8-1.0). Secondary "
    "roles are good fits (0.5-0.8). Adjacent roles are stretches (0.3-0.5). "
    "Roles outside all three buckets start at 0.0-0.3.\n\n"
    "2. SENIORITY ALIGNMENT: Does the role's level match the candidate's "
    "archetype.level (e.g. 'Senior/Staff', 'Mid-Senior')? Mismatched "
    "seniority is a strong negative signal even when the role family "
    "is a perfect match.\n\n"
    "3. SKILL MATCH: Cross-reference the opportunity's required skills "
    "against the candidate's superpowers and proof_points. A direct "
    "match to a superpower is a strong positive; a match to a "
    "proof_point's hero_metric is even stronger (concrete proof of "
    "impact at scale).\n\n"
    "4. NARRATIVE ALIGNMENT: Does the role's mission/responsibilities "
    "align with the candidate's headline and exit_story? The narrative "
    "tells you WHAT the candidate is optimizing for, not just WHAT "
    "they can do — a role that matches the exit_story scores higher "
    "than one that only matches the skills.\n\n"
    "5. COMPENSATION: If the posting mentions a salary range, check "
    "against the candidate's compensation.target_range and minimum. A "
    "posting below the minimum is a soft mismatch even if the role is "
    "a perfect fit; surface this in the reasoning.\n\n"
    "6. LOCATION: Does the role's location/remote policy match the "
    "candidate's location.city / location.timezone / location.visa_status? "
    "A posting that hard-requires sponsorship when visa_status says "
    "'No sponsorship needed' is a hard mismatch — score 0.0-0.2.\n\n"
    "7. PROOF POINTS: If the candidate has proof_points with hero_metrics, "
    "look for similar metrics in the posting (scale, impact, users). A "
    "match here signals the candidate has done this exact kind of work.\n\n"
    "Return ONLY the JSON object, with no preamble, no markdown. The "
    "score must be calibrated across these factors — a 0.9 posting "
    "scores 0.9 because it matches on 5+ factors, not because the "
    "title alone is close. 0.0 = clearly off-profile on multiple "
    "factors, 1.0 = exact match on all factors."
)

# System prompt for the resume → profile extractor. Mirrors the YAML
# schema in ``config/profile.example.yml`` so the parsed dict can be
# passed directly to ``Profile(**data)`` without field renaming. The
# LLM is told to OMIT unknown fields rather than emit nulls — that
# keeps ``Profile.model_dump(exclude_none=True)`` honest on save
# (otherwise every field would render as ``key: null`` and operators
# couldn't tell at-a-glance which sections the LLM actually filled).
PROFILE_EXTRACTION_SYSTEM_PROMPT: Final = (
    "You are a career-ops profile extractor. Given a candidate's resume "
    "text, output strict JSON that mirrors the JobRadar profile schema. "
    "All fields are optional; OMIT any field the resume does not "
    "explicitly state. Do NOT use null — just leave the key out.\n\n"
    "Use this exact shape (keys, not values):\n"
    "{\n"
    '  "candidate": {"full_name", "email", "phone", "location", '
    '"linkedin", "portfolio_url", "github", "twitter"},\n'
    '  "target_roles": {\n'
    '    "primary": ["Senior AI Engineer", "Staff ML Engineer"],\n'
    '    "archetypes": [\n'
    '      {"name": "AI/ML Engineer", "level": "Senior/Staff", '
    '"fit": "primary|secondary|adjacent"}\n'
    "    ]\n"
    "  },\n"
    '  "narrative": {\n'
    '    "headline": "One-line professional headline",\n'
    '    "exit_story": "What makes this candidate unique",\n'
    '    "superpowers": ["3-5 concrete strengths"],\n'
    '    "proof_points": [{"name", "url", "hero_metric"}]\n'
    "  },\n"
    '  "compensation": {"target_range", "currency", "minimum", '
    '"location_flexibility"},\n'
    '  "location": {"country", "city", "timezone", "visa_status"}\n'
    "}\n\n"
    "Rules:\n"
    "- Only extract what the resume EXPLICITLY says. Do not invent "
    "roles, projects, or metrics.\n"
    "- target_roles.primary: 2-4 specific role titles the candidate is "
    "optimizing for (drawn from current/recent titles + stated goals).\n"
    "- target_roles.archetypes: 2-4 role families with fit levels "
    "(primary = dream role, secondary = good fit, adjacent = stretch).\n"
    "- superpowers: 3-5 concrete skills/strengths, not generic soft "
    "skills (e.g. 'PyTorch' yes, 'team player' no).\n"
    "- proof_points: 2-4 projects/articles with measurable impact IF "
    "the resume lists them.\n"
    "- Return ONLY the JSON object. No preamble, no markdown fences, "
    "no commentary."
)

# ``max_tokens`` for the profile extraction is larger than the score
# call because the LLM has to render a full profile JSON (often 1.5-3 KB
# for a senior candidate with multiple projects). 2500 is generous
# enough for the worst-case resume the operator is likely to upload
# without bloating the per-token cost when most resumes fit in 800-1200
# tokens.
PROFILE_EXTRACTION_MAX_TOKENS: Final = 2500

# System prompt for :meth:`LLMClient.pick_best_resume`. Strict-JSON
# output shape with ``resume_id`` (``null`` declines cleanly) +
# ``confidence`` (0.0-1.0) so the matcher can read it with
# ``_parse_resume_pick_response`` without prose-in-prose regex
# fallback. Reasoning is at most 400 chars to keep NVIDIA budget
# honest when the apply worker scans hundreds of jobs per hour.
_PICK_RESUME_SYSTEM_PROMPT: Final = (
    "You are a resume-matcher assistant. Given a single job posting "
    "and a lean list of candidate resumes (id + name + tags + is_default "
    "flag only — full resume text was stripped to control token cost), "
    "pick the single resume that best matches the role. Output strict "
    "JSON, no markdown, no preamble:\n\n"
    '{"resume_id": "<one of the candidate ids>", "confidence": <0.0-1.0>, '
    '"reasoning": "<at most 400 chars>"}\n\n'
    "Match logic:\n"
    "1. If the job's role family matches a candidate's tags (e.g. "
    "'production-ai' tag on an 'AI Platform Engineer' job), pick that "
    "resume even with low tag-overlap count — the operator spent the "
    "time curating tags precisely for this match.\n"
    "2. If multiple resumes have matching tags, prefer the one whose "
    "name is most clearly aligned with the job title.\n"
    "3. If NO resume has matching tags AND the job is for a domain "
    "the candidate doesn't appear to work in, return "
    '{"resume_id": null, "confidence": 0.0, "reasoning": "..."} so the '
    "operator can manually pick a resume via the QABank UI rather than "
    "submitting with a wrong fit.\n\n"
    "Never force a match if no candidate is genuinely aligned. "
    "Returning null is preferred over a forced match that doesn't "
    "actually fit."
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
    """Backward-compat single-bucket helper.

    Older callers (and the scoring_service module's pre-2-key code
    path) want to fetch ONE bucket sized at ``NVIDIA_RPM``. New code
    should compose :func:`_make_nvidia_bucket` directly so the bucket
    capacity is explicit. Kept as a thin wrapper so the signature
    stays stable.
    """
    raw = os.environ.get("NVIDIA_RPM", str(DEFAULT_NVIDIA_RPM)).strip()
    try:
        rpm = int(raw or DEFAULT_NVIDIA_RPM)
    except ValueError as exc:
        raise ValueError(
            f"NVIDIA_RPM={raw!r} is not a valid integer (expected a "
            f"non-negative request-per-minute budget, e.g. '40', '0')."
        ) from exc
    if rpm <= 0:
        return None
    return _make_nvidia_bucket(rpm)


def _unique_rate_limiters(providers: list[ProviderConfig]) -> list[AsyncTokenBucket]:
    """Dedupe rate-limiters by object identity.

    Multiple :class:`ProviderConfig` slots can share one
    :class:`AsyncTokenBucket` (the 2-NVIDIA-key case). Without this
    dedupe an opportunity that walks across both NVIDIA providers
    would ``acquire()`` the same bucket twice, halving the effective
    throughput from the operator's intended
    ``len(nvidia_keys) * 40`` RPM down to ``40`` RPM.

    Returns the buckets in first-seen order so the acquire sequence
    is deterministic (helpful for log replays).
    """
    seen_ids: set[int] = set()
    out: list[AsyncTokenBucket] = []
    for provider in providers:
        bucket = provider.rate_limiter
        if bucket is None:
            continue
        bid = id(bucket)
        if bid in seen_ids:
            continue
        seen_ids.add(bid)
        out.append(bucket)
    return out


def _make_nvidia_bucket(rpm: int) -> AsyncTokenBucket:
    """Return (or build + cache) the token bucket for a given RPM target.

    The cache key is the integer RPM, so a single FastAPI process
    serves all its LLMClients from one bucket per RPM value — even
    when those clients correspond to multiple NVIDIA API keys.

    A non-positive ``rpm`` is normalised to a no-op helper on the
    side: callers that pass 0 from a misconfig should NOT see a
    bucket created (it would block ``acquire()`` forever per the
    bucket invariant). We raise ``ValueError`` instead so the
    misconfig surfaces at route import time rather than the first
    LLM call.
    """
    if rpm <= 0:
        raise ValueError(
            f"cannot build a rate-limiter bucket for rpm={rpm}; pass a "
            f"positive integer (the LLMClient.from_env() code short-circuits "
            f"to None when NVIDIA_RPM is 0)"
        )
    bucket = _NVIDIA_RPM_LIMITERS.get(rpm)
    if bucket is None:
        bucket = AsyncTokenBucket(capacity=rpm, refill_per_second=rpm / 60.0)
        _NVIDIA_RPM_LIMITERS[rpm] = bucket
        logging.getLogger("jobradar.llm").info(
            "NVIDIA rate-limiter bucket active: capacity=%d (refill=%.4f/s). "
            "Module-level cache so multiple LLMClients + multiple NVIDIA "
            "keys share one process-global budget. Override with NVIDIA_RPM=0 "
            "to disable.",
            rpm,
            rpm / 60.0,
        )
    return bucket


class ProviderConfig(NamedTuple):
    """One slot in the LLM provider chain.

    ``rate_limiter`` is an optional :class:`AsyncTokenBucket`; when set,
    :meth:`LLMClient.score_opportunity` will ``await acquire()`` before
    the first attempt at this provider.

    When the operator configures two NVIDIA API keys (``NVIDIA_API_KEY``
    AND ``NVIDIA_API_KEY_2``), both keys feed the chain as separate
    providers (a primary ``"nvidia"`` slot + a ``"nvidia_2"`` slot) —
    but they share a *single* token bucket sized at
    ``len(nvidia_keys) * 40 RPM``. A 401 on the first key breaks out of
    the inner retry loop and advances to the second key, which then
    ``await``s the same shared bucket. The end result is "doubled RPM
    throughput on a healthy pair of keys, transparent fail-over if one
    of them gets revoked".
    """

    name: str
    base_url: str
    api_key: str
    model: str
    rate_limiter: AsyncTokenBucket | None = None

    # ``key_label`` disambiguates the two NVIDIA slots in logs (``nvidia``
    # vs ``nvidia_2``) and in the per-key future-disable list. None for
    # the single NVIDIA case so the logs clean up.
    key_label: str | None = None


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
        NVIDIA first (one provider per configured key), Groq second so
        the cheaper/quicker primary is tried first by default.

        NVIDIA key plumbing: ``NVIDIA_API_KEY`` is the canonical env
        var; ``NVIDIA_API_KEY_2`` is the optional second-key slot. When
        both are present the chain becomes
        ``[nvidia_1, nvidia_2, groq]`` and they share a single token
        bucket sized at ``len(keys) * NVIDIA_RPM`` (so two keys at the
        default 40 RPM yield an 80 RPM combined budget, exactly the
        "doubled RPM" behaviour the operator asked for).

        The shared bucket is *one* per (RPM, providers-config) tuple so a
        FastAPI process that constructs multiple :class:`LLMClient`
        instances still respects the global provider RPM — the module-
        level :data:`_NVIDIA_RPM_LIMITERS` cache keys by RPM value
        only, so two LLMClients reading the same env see the same
        bucket.

        NVIDIA is throttled by an :class:`AsyncTokenBucket` whose
        capacity and refill rate are derived from the ``NVIDIA_RPM``
        env var (default :data:`DEFAULT_NVIDIA_RPM`, i.e. 40 RPM per
        key). Set ``NVIDIA_RPM=0`` to disable the limiter entirely —
        useful for higher tier keys, integration tests, and one-off
        bulk rescans.
        """
        providers: list[ProviderConfig] = []

        # ---- Build the list of NVIDIA keys, in priority order. ----------
        # ``NVIDIA_API_KEY`` is canonical; ``NVIDIA_API_KEY_2`` is the
        # opt-in second key. Empty strings are filtered out so a
        # placeholder env file (``NVIDIA_API_KEY_2=``) doesn't sneak a
        # blank slot into the chain.
        nvidia_keys: list[tuple[str, str, str]] = []  # (env_name, key, label)
        for env_name, label in (("NVIDIA_API_KEY", "primary"), ("NVIDIA_API_KEY_2", "secondary")):
            value = os.environ.get(env_name, "").strip()
            if value:
                nvidia_keys.append((env_name, value, label))

        if nvidia_keys:
            # ``len(keys) * DEFAULT_NVIDIA_RPM`` — the doubling the
            # operator asked for, capped at the bucket's
            # ``NVIDIA_RPM`` env override. Wrap the int() so a
            # malformed env value (e.g. ``NVIDIA_RPM=fast``) raises
            # a clear ``ValueError("NVIDIA_RPM=...")`` rather than
            # the stdlib ``int()`` error that doesn't name the env
            # var — the operator reading worker logs at boot
            # immediately sees which knob is mis-configured.
            raw_rpm = os.environ.get("NVIDIA_RPM", str(DEFAULT_NVIDIA_RPM)).strip()
            try:
                rpm_per_key = int(raw_rpm or DEFAULT_NVIDIA_RPM)
            except ValueError as exc:
                raise ValueError(
                    f"NVIDIA_RPM={raw_rpm!r} is not a valid integer: {exc}. "
                    f"Expected a non-negative integer (e.g. '40', '0', '100')."
                ) from exc
            total_rpm = rpm_per_key * len(nvidia_keys)
            # ``NVIDIA_RPM=0`` (or a malformed-but-zero value) means
            # "disable throttling" — the operator wants to push the
            # throttle out of the way for a higher-tier key or a
            # one-off bulk rescan. Without this short-circuit
            # ``_make_nvidia_bucket(0)`` raises ``ValueError`` per
            # its positive-integer contract; we keep the providers
            # in the chain with ``rate_limiter=None`` so the chain
            # still works (each call fires immediately, no 429
            # protection — but that's the explicit intent).
            shared_bucket = (
                _make_nvidia_bucket(total_rpm) if total_rpm > 0 else None
            )
            base_url = os.environ.get("NVIDIA_BASE_URL", DEFAULT_NVIDIA_BASE)
            model = os.environ.get("NVIDIA_MODEL", DEFAULT_NVIDIA_MODEL)
            for idx, (_env_name, key, _label) in enumerate(nvidia_keys):
                slot_name = "nvidia" if idx == 0 else f"nvidia_{idx + 1}"
                providers.append(
                    ProviderConfig(
                        name=slot_name,
                        base_url=base_url,
                        api_key=key,
                        model=model,
                        rate_limiter=shared_bucket,
                        key_label=slot_name,
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

    async def research_opportunity(
        self,
        job: dict,
        profile_summary: str,
        websearch_results: list[dict] | None = None,
    ) -> tuple[str, str]:
        """Generate an interview-prep Markdown brief for a single job.

        Returns ``(markdown_content, model_used)`` where ``model_used``
        is the ``ProviderConfig.model`` string of whichever provider
        produced the response (useful for cost-tagging and for the
        front-end display '\u2014 generated by NVIDIA llama-3.1-70b\u2019).

        ``websearch_results`` is reserved for the LLM + Serper /
        Apify future. v1 callers pass ``None``; the prompt builder
        recognises the empty case and slots the right fallback text.

        Same per-provider retry chain + shared NVIDIA bucket contract
        as :meth:`score_opportunity`. The cost ceiling is the same
        (``len(providers) * 2`` calls). The async research UX
        described in the design spec is intentionally NOT wired here
        \u2014 v1 routes call this sync and the route handler awaits
        before returning.
        """
        prompt = build_research_prompt(job, profile_summary, websearch_results)
        last_exc: Exception | None = None
        for provider in self.providers:
            if provider.rate_limiter is not None:
                await provider.rate_limiter.acquire()
            client = self._clients[provider.name]
            for attempt in (1, 2):
                try:
                    content = await self._research_once(
                        client, provider.model, prompt
                    )
                    return content, provider.model
                except _PermanentError as exc:
                    last_exc = exc
                    break  # unrecoverable on this provider; advance
                except Exception as exc:  # noqa: BLE001 \u2014 transient
                    last_exc = exc
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                    # else: fall through to next provider
        raise RuntimeError(
            f"all LLM providers failed on research call; last error "
            f"type={type(last_exc).__name__}"
        ) from last_exc

    async def _research_once(
        self,
        client: AsyncOpenAI,
        model: str,
        prompt: str,
    ) -> str:
        """One non-retrying research call. Same error taxonomy as ``_score_once``.

        ``RateLimitError`` is treated as PERMANENT on the current
        provider so the chain advances immediately without the 0.5s
        ``asyncio.sleep`` retry — see :meth:`_score_once` for the full
        rationale (the same fix that makes both NVIDIA keys walk
        the chain without burning sleep against exhausted buckets).
        """
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,  # slightly creative\u2014the brief has prose sections
                max_tokens=RESEARCH_MAX_TOKENS,
            )
        except (AuthenticationError, BadRequestError, NotFoundError, RateLimitError) as exc:
            raise _PermanentError(f"{type(exc).__name__}: {exc}") from exc
        except (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        ):
            raise  # transient; caller decides retry vs advance
        return (resp.choices[0].message.content or "").strip()

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
        # Acquire each UNIQUE rate-limiter bucket at most once per
        # opportunity. Two NVIDIA providers reference the SAME bucket
        # when NVIDIA_API_KEY and NVIDIA_API_KEY_2 are both set; if we
        # blindly acquired per-provider-iteration each opportunity
        # would consume 2 tokens and the effective throughput would be
        # half the intended ``len(nvidia_keys) * 40`` RPM.
        for bucket in _unique_rate_limiters(self.providers):
            await bucket.acquire()
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
        """One non-retrying score call.

        ``RateLimitError`` is treated as PERMANENT on the current
        provider so the chain advances immediately without the 0.5s
        ``asyncio.sleep`` retry — a key whose bucket is exhausted
        won't recover in 0.5s, and the burning sleep just delays
        the failover to ``nvidia_2`` / ``groq`` in the chain. With
        two NVIDIA keys configured (``NVIDIA_API_KEY`` +
        ``NVIDIA_API_KEY_2``), this turns the operator's reported
        ``all LLM providers failed; last error type=RateLimitError``
        into a fast walk across both keys before the chain gives up.
        """
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
        except (AuthenticationError, BadRequestError, NotFoundError, RateLimitError) as exc:
            raise _PermanentError(f"{type(exc).__name__}: {exc}") from exc
        except (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        ):
            raise  # transient; caller decides retry vs advance
        content = (resp.choices[0].message.content or "").strip()
        return parse_score_response(content)

    async def extract_profile(self, resume_text: str) -> tuple[dict, str]:
        """Run the resume → profile extractor on the given resume text.

        Returns ``(profile_dict, model_used)`` where ``profile_dict`` is
        a plain ``dict`` shaped like the ``Profile`` Pydantic model
        (caller validates + saves). ``model_used`` is the
        ``ProviderConfig.model`` string of whichever provider
        produced the response (useful for cost-tagging and the
        ``POST /api/resumes`` upload-side-effect log line).

        Same per-provider retry chain + shared NVIDIA bucket contract
        as :meth:`score_opportunity` and :meth:`research_opportunity`.
        Cost ceiling: ``len(providers) * 2`` LLM calls per resume.
        Total token budget is :data:`PROFILE_EXTRACTION_MAX_TOKENS`
        (2500) — generous enough for a senior candidate with several
        projects, bounded enough that one big extraction doesn't
        blow a single response.

        The returned dict is NOT validated here. The caller
        (typically :func:`services.profile_service.extract_profile_from_resume`)
        is responsible for ``Profile(**data)`` and the disk save, so
        an extraction that returns a partial dict (e.g. LLM only
        filled ``target_roles``) still validates as an empty-rest
        ``Profile()`` rather than failing the call. The same
        shape-on-the-wire guarantee is the one we already use for
        ``parse_score_response`` — keep validation lazy.
        """
        last_exc: Exception | None = None
        # Same acquire-once-per-unique-bucket contract as
        # :meth:`score_opportunity` — see that method's docstring for
        # the two-NVIDIA-keys rationale.
        for bucket in _unique_rate_limiters(self.providers):
            await bucket.acquire()
        for provider in self.providers:
            client = self._clients[provider.name]
            for attempt in (1, 2):
                try:
                    return await self._extract_profile_once(
                        client, provider.model, resume_text
                    ), provider.model
                except _PermanentError as exc:
                    last_exc = exc
                    break  # unrecoverable on this provider; advance
                except Exception as exc:  # noqa: BLE001 — transient
                    last_exc = exc
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                    # else: fall through to next provider
        raise RuntimeError(
            f"all LLM providers failed on profile extraction; last error "
            f"type={type(last_exc).__name__}"
        ) from last_exc

    async def _extract_profile_once(
        self,
        client: AsyncOpenAI,
        model: str,
        resume_text: str,
    ) -> dict:
        """One non-retrying profile-extraction call.

        Mirrors :meth:`_score_once` / :meth:`_research_once`: same
        error taxonomy, same retry-vs-advance contract, but a higher
        ``max_tokens`` ceiling because the LLM has to render a full
        profile JSON (typically 1-3 KB). ``RateLimitError`` advances
        immediately (treated as permanent — see ``_score_once``).
        """
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": PROFILE_EXTRACTION_SYSTEM_PROMPT,
                    },
                    {"role": "user", "content": resume_text},
                ],
                temperature=0.0,
                max_tokens=PROFILE_EXTRACTION_MAX_TOKENS,
            )
        except (AuthenticationError, BadRequestError, NotFoundError, RateLimitError) as exc:
            raise _PermanentError(f"{type(exc).__name__}: {exc}") from exc
        except (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        ):
            raise  # transient; caller decides retry vs advance
        content = (resp.choices[0].message.content or "").strip()
        return parse_profile_response(content)

    async def pick_best_resume(
        self,
        job_payload: dict,
        resume_payloads: list[dict],
    ) -> tuple[str | None, float]:
        """Ask the LLM which resume best matches a single job posting.

        Used by :mod:`apply_worker.resume_picker` as the tag-match
        fallback. Returns ``(resume_id, confidence)`` where
        ``resume_id`` is the chosen resume's ``id`` string, or
        ``None`` when the LLM declines (returns ``{"resume_id":
        null, ...}``). ``confidence`` is the model's 0.0-1.0 self-
        reported confidence, clamped.

        Each provider is tried once with the standard transient-vs-
        permanent error taxonomy (:attr:`_unique_rate_limiters` is
        consumed once per unique bucket to keep the NVIDIA RPM
        budget honest on the two-key case). Permanently-broken
        responses (e.g. ``BadRequest``) advance to the next
        provider; transient failures retry once, then advance.

        Cost ceiling: ``len(providers) * 2`` LLM calls per job —
        same as :meth:`score_opportunity`. Expected hot path is
        the GHA boards-scan worker picking resumes for hundreds
        of fresh ASBY / Lever / Greenhouse wins per hour, so the
        cost stays within the operator's 40-RPM free tier when the
        per-job tag-match rate stays >50%.
        """
        user_prompt = json.dumps(
            {
                "job": job_payload,
                "resumes": [
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "tags": r.get("tags") or [],
                        "is_default": bool(r.get("is_default", False)),
                    }
                    for r in resume_payloads
                ],
            },
            indent=2,
        )
        last_exc: Exception | None = None
        for bucket in _unique_rate_limiters(self.providers):
            await bucket.acquire()
        for provider in self.providers:
            client = self._clients[provider.name]
            for attempt in (1, 2):
                try:
                    content = await self._pick_resume_once(
                        client, provider.model, user_prompt
                    )
                    parsed = _parse_resume_pick_response(
                        content, expected_ids=[str(r["id"]) for r in resume_payloads]
                    )
                    return parsed
                except _PermanentError as exc:
                    last_exc = exc
                    break  # unrecoverable on this provider; advance
                except Exception as exc:  # noqa: BLE001 — transient
                    last_exc = exc
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                    # else: fall through to next provider
        raise RuntimeError(
            f"all LLM providers failed on pick_best_resume; last error "
            f"type={type(last_exc).__name__}"
        ) from last_exc

    async def _pick_resume_once(
        self,
        client: AsyncOpenAI,
        model: str,
        user_prompt: str,
    ) -> str:
        """One non-retrying pick_best_resume call.

        ``max_tokens=300`` because the response is bounded: a single
        JSON object with ``resume_id`` + ``confidence`` is at most
        ~80 tokens. Bounding the cap defends against a model that
        streams analysis prose before the JSON. ``RateLimitError``
        advances immediately (treated as permanent — see
        ``_score_once``).
        """
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _PICK_RESUME_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=300,
            )
        except (AuthenticationError, BadRequestError, NotFoundError, RateLimitError) as exc:
            raise _PermanentError(f"{type(exc).__name__}: {exc}") from exc
        except (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        ):
            raise  # transient; caller decides retry vs advance
        return (resp.choices[0].message.content or "").strip()

    async def run_json_prompt(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 600,
        temperature: float = 0.0,
    ) -> tuple[str, str]:
        """Generic system+user chat-completions call with provider-chain retry.

        Surface for callers that need the retry-then-fallback contract
        but with their OWN system prompt (and don't want to add a new
        ``_xxx_once`` method per use case). Currently used by
        :mod:`apply_worker.qa_matcher` to run its batched JSON map
        prompt — the matcher calls this with ``max_tokens=600`` (its
        response shape is ``len(unmatched_fields)`` JSON entries, ~150
        chars total at 5 unmatched fields) and ``temperature=0`` for
        strict JSON determinism.

        Returns ``(content, model_used)``. Raises :class:`RuntimeError`
        when every provider fails — same contract as
        :meth:`score_opportunity`. ``max_tokens`` is plumbed through
        because the default ``600`` would be wasteful for tiny jobs
        (a 60-token pick) and would be a soft cap for bigger jobs
        (a 1200-token profile extraction) — let the caller size it.
        """
        last_exc: Exception | None = None
        for bucket in _unique_rate_limiters(self.providers):
            await bucket.acquire()
        for provider in self.providers:
            client = self._clients[provider.name]
            for attempt in (1, 2):
                try:
                    try:
                        resp = await client.chat.completions.create(
                            model=provider.model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            temperature=temperature,
                            max_tokens=max_tokens,
                        )
                    except (AuthenticationError, BadRequestError, NotFoundError, RateLimitError) as exc:
                        # ``RateLimitError`` advances immediately
                        # instead of wasting 0.5s on a retry that
                        # will hit the same exhausted bucket — see
                        # ``_score_once`` for the full rationale.
                        raise _PermanentError(
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
                    except (
                        APIConnectionError,
                        APITimeoutError,
                        InternalServerError,
                    ):
                        raise  # transient; caller decides retry vs advance
                    content = (resp.choices[0].message.content or "").strip()
                    return content, provider.model
                except _PermanentError as exc:
                    last_exc = exc
                    break
                except Exception as exc:  # noqa: BLE001 — transient
                    last_exc = exc
                    if attempt == 1:
                        await asyncio.sleep(0.5)
                    # else: fall through to next provider
        raise RuntimeError(
            f"all LLM providers failed on run_json_prompt; last error "
            f"type={type(last_exc).__name__}"
        ) from last_exc


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


def parse_profile_response(content: str) -> dict:
    """Parse the LLM's profile JSON response.

    Robust to:

    * Pure JSON — ``json.loads`` first.
    * Markdown code fences — ``\\`\\`\\`json ... \\`\\`\\``` stripped.
    * The model wrapping the JSON in preamble prose — fall back to
      **string slicing** to extract the outermost ``{...}`` block.
      Slicing beats regex here because the profile JSON has nested
      objects (``target_roles.archetypes``, ``narrative.proof_points``)
      that a flat character class would mis-match. ``str.find("{")`` +
      ``str.rfind("}")`` gives us the outermost braces in one pass.

    Returns the parsed ``dict``. Raises :class:`_PermanentError` if
    no parse path produces a JSON object — same contract as
    :func:`parse_score_response`. The caller is responsible for
    Pydantic validation against :class:`services.profile_service.Profile`
    so partial dicts (e.g. LLM only filled ``target_roles``) still
    validate as an empty-rest ``Profile()``.
    """
    text = content.strip()
    if text.startswith("```"):
        # ``strip("`\\n ")`` is order-sensitive on purpose: backticks
        # first, then whitespace. The first 4 chars of a ``json``
        # fence would survive if we went whitespace-first; ordering
        # them like this keeps the post-strip prefix detect clean.
        text = text.strip("`\n ")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    # Direct parse — the common case when the model obeyed the
    # "ONLY the JSON object" rule in PROFILE_EXTRACTION_SYSTEM_PROMPT.
    try:
        return dict(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # String-slicing fallback for the prose-wrapped case. We use the
    # OUTERMOST ``{...}`` so nested objects (archetypes, proof_points)
    # stay intact. If the model emitted multiple JSON objects we
    # would over-capture; the system prompt forbids that and the
    # alternative is to add a JSON-stack parser for a 1-in-1000 case.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return dict(json.loads(candidate))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    raise _PermanentError(
        f"could not parse profile response: {content[:200]!r}"
    )


def _coerce_score(data: dict) -> tuple[float, str]:
    score = float(data["score"])
    score = max(0.0, min(1.0, score))
    reasoning = str(data["reasoning"])[:400]
    return score, reasoning


def _parse_resume_pick_response(
    content: str,
    *,
    expected_ids: list[str],
) -> tuple[str | None, float]:
    """Parse the strict-JSON ``{resume_id, confidence, reasoning}`` envelope.

    Hallucinated ids (``resume_id`` not in ``expected_ids``) collapse
    to ``(None, 0.0)`` so the apply worker surfaces the no-match as
    "operator must pick manually" rather than submitting with an id
    the matcher can't validate.

    Markdown code fences (``\\`\\`\\`json ... \\`\\`\\```) are stripped
    first, then ``json.loads`` is attempted, then string-sliced to
    the outermost ``{...}`` as a last-ditch fallback. The same
    parse path is used for :func:`parse_score_response` /
    :func:`parse_profile_response` so the operator's template
    prompt repo is the single source of truth for "what JSON shape
    do I expect from the model".
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n ")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    allowed = set(expected_ids)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise _PermanentError(
                f"could not parse pick_best_resume envelope: {content[:200]!r}"
            )
        try:
            data = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise _PermanentError(
                f"could not parse pick_best_resume envelope: {content[:200]!r}"
            ) from exc
    if not isinstance(data, dict):
        raise _PermanentError(
            f"pick_best_resume response was not a JSON object: {content[:200]!r}"
        )
    raw_id = data.get("resume_id")
    if raw_id is None:
        confidence = float(data.get("confidence", 0.0) or 0.0)
        return None, max(0.0, min(1.0, confidence))
    if not isinstance(raw_id, str) or raw_id not in allowed:
        # Hallucinated id or non-string — collapse to None so the
        # apply worker treats it as a clean refusal rather than a
        # match against an arbitrary external id.
        return None, 0.0
    confidence = float(data.get("confidence", 0.0) or 0.0)
    return raw_id, max(0.0, min(1.0, confidence))


# ---------------------------------------------------------------------------
# Interview-Prep "deep research" surface.
#
# The Interview Prep card on the JobBoard darkens a regular scoring call
# with a larger context window and a Markdown-flavoured prompt so the
# LLM can produce a structured pre-interview brief. v1 is pure-LLM;
# the ``websearch_results`` kwarg is plumbed but unused so a future
# Serper / Apify integration can drop in without touching the route.
# ---------------------------------------------------------------------------
RESEARCH_SYSTEM_PROMPT: Final = (
    "You are an expert interview-prep analyst. Given a single job "
    "posting and (optionally) recent web search results about the "
    "company, produce a structured pre-interview brief in Markdown. "
    "Use exactly these sections, in this order:\n\n"
    "## Company Snapshot\n"
    "## Likely Tech Stack\n"
    "## What they probably test\n"
    "## 5 smart questions to ask them\n"
    "## Red flags / watch-outs\n\n"
    "Be specific. Use the role description verbatim where it names "
    "frameworks, services, level. Highlight anything the candidate "
    "should highlight in the opener. If a section cannot be answered "
    "from the inputs, write 'Not enough public info' rather than "
    "guessing. Return Markdown only — no JSON, no preamble."
)


def build_research_prompt(
    job: dict,
    profile_summary: str,
    websearch_results: list[dict] | None = None,
) -> str:
    """Compose the user-side prompt for :meth:`LLMClient.research_opportunity`.

    Inputs:
    * ``job`` — the Job/Company dict (title, company_name, url, description+).
    * ``profile_summary`` — same target-roles + Q&A blob the scorer uses so the
      brief can call out specific skill matches / conflicts.
    * ``websearch_results`` — reserved. When non-None each entry is a dict
      shaped ``{"title", "url", "snippet"}``; appended verbatim to the prompt
      under a "Web context" section. v1 always passes None.

    The output caps ``description`` at 1500 chars because LLM context
    windows are finite and a 10 KB posting description would crowd
    out the analysis room. The cap is intentionally generous — most
    real postings arrive in 1-3 KB.
    """
    pieces: list[str] = [
        "Candidate profile (use to flag skill matches and gaps):",
        profile_summary.strip(),
        "",
        "Job posting:",
        f"Title: {job.get('title') or '(untitled)'}",
        f"Company: {job.get('company_name') or '(unknown)'}",
        f"URL: {job.get('url') or '(no url)'}",
        f"ATS / source: {job.get('ats_type') or job.get('source') or '(unknown)'}",
        "",
        "Description (truncated to 1500 chars):",
        (job.get("description") or "(no description)")[:1500],
    ]
    if websearch_results:
        pieces.extend(["", "Web context (most recent first):"])
        for result in websearch_results[:5]:
            pieces.append(
                f"- {result.get('title', '(untitled)')}\n"
                f"  {result.get('url', '')}\n"
                f"  {result.get('snippet', '')[:300]}"
            )
    pieces.extend(
        [
            "",
            "Return Markdown only, with the five sections named above.",
        ]
    )
    return "\n".join(pieces)


# ``max_tokens`` for the research brief is larger than the score call
# because a five-section Markdown brief can easily run 700-1200 tokens.
RESEARCH_MAX_TOKENS: Final = 1500


__all__ = [
    "DEFAULT_NVIDIA_BASE",
    "DEFAULT_NVIDIA_MODEL",
    "DEFAULT_GROQ_BASE",
    "DEFAULT_GROQ_MODEL",
    "DEFAULT_NVIDIA_RPM",
    "SYSTEM_PROMPT",
    "RESEARCH_SYSTEM_PROMPT",
    "RESEARCH_MAX_TOKENS",
    "PROFILE_EXTRACTION_SYSTEM_PROMPT",
    "PROFILE_EXTRACTION_MAX_TOKENS",
    "AsyncTokenBucket",
    "ProviderConfig",
    "LLMClient",
    "build_prompt",
    "build_research_prompt",
    "parse_score_response",
    "parse_profile_response",
    "_unique_rate_limiters",
    "_make_nvidia_bucket",
]  
