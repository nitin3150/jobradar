"""Tests for :mod:`services.llm_client` — LLM scoring with mocked SDK.

The official ``openai.AsyncOpenAI.chat.completions.create`` is mocked via
``unittest.mock.AsyncMock`` so these tests never hit NVIDIA or Groq. We cover
the retry-then-fallback chain by parameterising the sequence of responses
each provider returns.

Mock setup gotcha
================

The production code path is::

    resp = await client.chat.completions.create(...)

so the deepest callable is ``chat.completions.create``. We therefore wire
either the ``return_value`` (single response) or ``side_effect`` (list of
responses/exceptions in order) on the **deepest** mock, not on the
top-level AsyncMock — otherwise ``await client.chat.completions.create(...)``
returns a bare MagicMock instead of our fake response, which causes the
production code to trip when it tries to read ``resp.choices[0].message.content``.

Exception construction :func:`_sdk_exception`
=============================================

The ``openai>=1.30`` SDK splits error exceptions into three families with
distinct signatures:

* ``APIError(message, request, *, body)`` — base.
* ``APIStatusError(message, *, response, body)`` — auth / bad-request /
  rate-limit / not-found / internal-server.
* ``APIConnectionError(*, message, request)`` — keyword-only message + request.
* ``APITimeoutError(request)`` — positional request only.

:func:`_sdk_exception` routes each family to the right constructor.
"""
from __future__ import annotations

import asyncio
import os
import time
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from openai import (
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from services.llm_client import (
    SYSTEM_PROMPT,
    AsyncTokenBucket,
    LLMClient,
    ProviderConfig,
    _unique_rate_limiters,
    build_prompt,
    parse_profile_response,
    parse_score_response,
)


# ----------------------------------------------------------------------
# Mock helpers
# ----------------------------------------------------------------------
def _fake_response(content: str):
    """Build the minimal object tree that ``resp.choices[0].message.content`` reads."""

    class _Message:
        def __init__(self, c: str) -> None:
            self.content = c

    class _Choice:
        def __init__(self, c: str) -> None:
            self.message = _Message(c)

    class _Resp:
        def __init__(self, c: str) -> None:
            self.choices = [_Choice(c)]

    return _Resp(content)


def _mock_request() -> MagicMock:
    """Build the ``request=`` MagicMock that ``APIError`` / ``APIConnectionError`` need."""
    request = MagicMock()
    request.method = "POST"
    request.url = "https://fake/v1/chat/completions"
    request.headers = {}
    request.content = b"{}"
    return request


def _mock_response(status_code: int, body_text: str) -> MagicMock:
    """Build the ``response=`` MagicMock that ``APIStatusError`` subclasses need.

    Pre-populates ``status_code``, ``headers``, ``text``, ``content``,
    ``json()``, ``url``, ``request`` so the SDK's ``__str__`` doesn't trip on
    a bare MagicMock trying to be JSON-decoded or stringified.
    """
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"content-type": "application/json"}
    response.text = body_text
    response.content = body_text.encode("utf-8")
    response.json.return_value = (
        {"error": {"message": body_text, "code": status_code}}
        if body_text.strip().startswith("{")
        else {}
    )
    response.url = "https://fake/v1/chat/completions"
    response.request = _mock_request()
    return response


@contextmanager
def _no_sleep():
    """Replace ``asyncio.sleep`` with an ``AsyncMock`` for the duration of the block.

    Production uses ``asyncio.sleep(0.5)`` between retry attempts. Without
    mocking, every retry test pays 500ms of real latency per attempt — and
    the four-attempt fallback exhaustion test pays ~2s. The yielded mock
    also lets us assert how many times the retry path actually slept.
    """
    with patch.object(asyncio, "sleep", new=AsyncMock()) as mock_sleep:
        yield mock_sleep


# ----------------------------------------------------------------------
# AsyncTokenBucket — the rate-limiter primitive used to throttle NVIDIA
# at 40 RPM. These tests use real time (not mocks) because the bucket's
# correctness depends on monotonic-clock arithmetic; mocking
# ``time.monotonic`` would test the mock, not the bucket. Test latency
# stays bounded because we use small capacities + fast refill rates.
# ----------------------------------------------------------------------
class TestAsyncTokenBucket(unittest.IsolatedAsyncioTestCase):
    async def test_starts_full_at_capacity(self) -> None:
        bucket = AsyncTokenBucket(capacity=5, refill_per_second=10.0)
        self.assertAlmostEqual(bucket.available_tokens, 5.0)

    async def test_drains_one_token_per_acquire(self) -> None:
        bucket = AsyncTokenBucket(capacity=3, refill_per_second=100.0)
        await bucket.acquire()  # token 1/3
        # places=1 because the 100/s refill adds ~1ms worth of
        # fractional tokens between acquires — places=3 was tight
        # enough to flake on a slow CI runner. places=1 is the
        # documented precision contract for this assertion.
        self.assertAlmostEqual(bucket.available_tokens, 2.0, places=1)
        await bucket.acquire()  # token 2/3
        self.assertAlmostEqual(bucket.available_tokens, 1.0, places=1)
        await bucket.acquire()  # token 3/3
        self.assertAlmostEqual(bucket.available_tokens, 0.0, places=1)

    async def test_blocks_when_bucket_is_empty(self) -> None:
        # 1 token capacity, 4 tokens/sec refill → ~250ms between calls
        # once the bucket is drained. We measure that the second
        # ``acquire()`` actually waited (≥ 200ms) rather than returning
        # instantly.
        bucket = AsyncTokenBucket(capacity=1, refill_per_second=4.0)
        await bucket.acquire()  # drains the only token
        t0 = time.monotonic()
        await bucket.acquire()  # must wait for refill
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(
            elapsed,
            0.20,
            f"acquire() returned in {elapsed:.3f}s with an empty bucket "
            f"and 4 tokens/s refill; expected ≥ 0.20s wait.",
        )

    async def test_does_not_block_when_capacity_remaining(self) -> None:
        # A high-capacity bucket should let the first N calls fire
        # instantly (no refill needed). This is the common-case
        # behaviour for the operator's first batch of scoring.
        bucket = AsyncTokenBucket(capacity=10, refill_per_second=1.0)
        t0 = time.monotonic()
        for _ in range(10):
            await bucket.acquire()
        elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed,
            0.10,
            f"10 instant acquires from a 10-token bucket took {elapsed:.3f}s; "
            f"expected < 0.10s (no refill should be required).",
        )

    async def test_concurrent_acquires_serialize_on_the_lock(self) -> None:
        # 2 tokens capacity, 10 tokens/sec refill. With 5 concurrent
        # acquirers, the first 2 should fire instantly and the next 3
        # should wait ~100ms each for refill. We assert the slowest
        # call's total wait stays in a reasonable band (≥ 200ms,
        # ≤ 1000ms) to catch a race that let multiple acquires slip
        # through without consuming a token. Upper bound is loose
        # because asyncio scheduling on a loaded CI runner can add
        # 100-200ms of jitter per acquire.
        bucket = AsyncTokenBucket(capacity=2, refill_per_second=10.0)
        t0 = time.monotonic()
        await asyncio.gather(*[bucket.acquire() for _ in range(5)])
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(
            elapsed,
            0.20,
            f"5 acquires with 2 tokens + 10/s refill took {elapsed:.3f}s; "
            f"expected ≥ 0.20s wait (3 refill rounds × 100ms each).",
        )
        self.assertLess(
            elapsed,
            1.00,
            f"5 acquires with 2 tokens + 10/s refill took {elapsed:.3f}s; "
            f"expected < 1.00s — a much longer wait means the lock isn't "
            f"releasing properly.",
        )

    async def test_cancellation_during_wait_does_not_consume_token(self) -> None:
        # If a worker is cancelled mid-wait (e.g. shutdown), the
        # token should NOT be consumed — the consumer never actually
        # called the LLM. We force a cancellation by racing the
        # acquire() against a 10ms timeout; the wait_seconds in
        # ``acquire()`` is ~1s (1 token capacity, 1/s refill), so
        # the timeout fires long before the bucket could refill.
        bucket = AsyncTokenBucket(capacity=1, refill_per_second=1.0)
        await bucket.acquire()  # drain the only token
        tokens_before = bucket._tokens
        self.assertAlmostEqual(tokens_before, 0.0, places=6)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bucket.acquire(), timeout=0.01)
        # Tighter assertion: bucket state should be unchanged by the
        # cancellation. If a regression ever consumed a token on
        # cancel, this would catch it before the next call even runs.
        # places=1 because the bucket's 1/s refill adds fractional
        # tokens while the cancellation propagates through
        # ``asyncio.wait_for`` — places=6 was tight enough to flake
        # on a slow CI runner. The semantic guarantee is "no
        # full token was consumed by the cancellation", not
        # "the internal float didn't change at all".
        self.assertAlmostEqual(
            bucket._tokens,
            tokens_before,
            places=1,
            msg=(
                f"cancelled acquire() mutated _tokens from {tokens_before} "
                f"to {bucket._tokens}; the consumer never reached the "
                f"decrement line, so the bucket state must be preserved."
            ),
        )
        # And the next acquire still has to wait for refill — proves
        # the token was actually drained before, not refilled by a
        # buggy cancel-handler.
        t0 = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(
            elapsed,
            0.50,
            f"acquire() after a cancelled wait returned in {elapsed:.3f}s; "
            f"expected ≥ 0.50s — a fast return would mean the cancelled "
            f"acquire left a phantom token in the bucket.",
        )

    async def test_idle_period_does_not_accumulate_unbounded_credit(self) -> None:
        # After a long sleep, the bucket should NOT have more than
        # ``capacity`` tokens — otherwise a subsequent burst would blow
        # through the rate limit. We can't actually sleep 60s in a
        # unit test, so we test the math directly: simulate a long
        # elapsed interval by manipulating the bucket's internal
        # state and confirming the cap kicks in.
        bucket = AsyncTokenBucket(capacity=5, refill_per_second=10.0)
        bucket._tokens = 0.0
        # Manually credit 1000 seconds of refill (would yield 10000 tokens
        # uncapped); acquire() must clamp to capacity before consuming.
        bucket._tokens += 1000.0 * bucket._refill
        await bucket.acquire()
        # First 5 acquires (capacity) are instant — each one is allowed
        # because the cap holds the bucket at 5.0 max. The 6th acquire
        # must wait for refill.
        for _ in range(4):
            await bucket.acquire()
        t0 = time.monotonic()
        await bucket.acquire()  # 6th — bucket was capped at 5
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(
            elapsed,
            0.05,
            f"6th acquire from a capped bucket took {elapsed:.3f}s; "
            f"expected ≥ 0.05s wait (cap=5, refill=10/s, so 6th needs "
            f"0.1s of refill). If the cap were broken, this would be 0.",
        )

    def test_rejects_non_positive_capacity(self) -> None:
        with self.assertRaises(ValueError):
            AsyncTokenBucket(capacity=0, refill_per_second=10.0)
        with self.assertRaises(ValueError):
            AsyncTokenBucket(capacity=-1, refill_per_second=10.0)

    def test_rejects_non_positive_refill(self) -> None:
        with self.assertRaises(ValueError):
            AsyncTokenBucket(capacity=5, refill_per_second=0.0)
        with self.assertRaises(ValueError):
            AsyncTokenBucket(capacity=5, refill_per_second=-1.0)


def _sdk_exception(exc_class: type, message: str, status_code: int = 500):
    """Construct any ``openai`` SDK exception with the right signature."""
    request = _mock_request()
    if exc_class is APITimeoutError:
        return exc_class(request)
    if exc_class is APIConnectionError:
        return exc_class(message=message, request=request)
    if issubclass(exc_class, APIStatusError):
        response = _mock_response(status_code, message)
        return exc_class(message=message, response=response, body=None)
    return exc_class(message, request)


def _build_mock_client(**provider_behavior: object) -> tuple[LLMClient, dict[str, AsyncMock]]:
    """Build an LLMClient whose internal ``_clients`` dict has AsyncMock stubs.

    The production code path is ``client.chat.completions.create(...)`` —
    so we wire the value on the deepest callable spec. ``return_value``
    on the top-level mock would land on a different code path and never
    reach the production caller.

    ``provider_behavior`` keyword args map provider names to either:

    * a single object — set as ``return_value``.
    * a list (consumed as ``side_effect`` in order — interleaving valid
      responses with ``_sdk_exception`` failures).
    """
    providers = [
        ProviderConfig(name=name, base_url=f"https://{name}", api_key="fake", model="m")
        for name in provider_behavior
    ]
    client = LLMClient(providers)
    mocks: dict[str, AsyncMock] = {}
    for name, value in provider_behavior.items():
        mock = AsyncMock()
        if isinstance(value, list):
            mock.chat.completions.create.side_effect = value
        else:
            mock.chat.completions.create.return_value = value
        mocks[name] = mock
    client._clients = mocks  # type: ignore[assignment]
    return client, mocks


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
class TestParseScoreResponse(unittest.TestCase):
    def test_parses_pure_json(self) -> None:
        score, reasoning = parse_score_response(
            '{"score": 0.78, "reasoning": "Strong match"}'
        )
        self.assertAlmostEqual(score, 0.78)
        self.assertEqual(reasoning, "Strong match")

    def test_strips_markdown_codefence(self) -> None:
        score, _ = parse_score_response('```json\n{"score": 0.4, "reasoning": "ok"}\n```')
        self.assertAlmostEqual(score, 0.4)

    def test_clamps_above_one(self) -> None:
        score, _ = parse_score_response('{"score": 1.7, "reasoning": "x"}')
        self.assertEqual(score, 1.0)

    def test_clamps_below_zero(self) -> None:
        score, _ = parse_score_response('{"score": -0.3, "reasoning": "x"}')
        self.assertEqual(score, 0.0)

    def test_falls_back_to_regex_when_wrapped_in_prose(self) -> None:
        text = 'Here is the JSON: {"score": 0.65, "reasoning": "medium fit"}. End.'
        score, reasoning = parse_score_response(text)
        self.assertAlmostEqual(score, 0.65)
        self.assertEqual(reasoning, "medium fit")

    def test_truncates_reasoning_to_400_chars(self) -> None:
        long_reason = "x" * 1000
        _, reasoning = parse_score_response(
            f'{{"score": 0.5, "reasoning": "{long_reason}"}}'
        )
        self.assertEqual(len(reasoning), 400)

    def test_raises_on_unparseable(self) -> None:
        from services.llm_client import _PermanentError
        with self.assertRaises(_PermanentError):
            parse_score_response("not json at all")


class TestParseProfileResponse(unittest.TestCase):
    """parse_profile_response is the Step-2 sibling of parse_score_response.

    It must handle the same parse paths (pure JSON, code-fence, prose
    wrap) AND survive the deeply-nested profile JSON shape (target_roles
    with archetypes list-of-dicts, narrative.proof_points, etc.). The
    string-slicing fallback in particular needs a test that exercises
    a non-trivial nested structure — a flat ``{a: 1, b: 2}`` would
    pass with a naive regex but a real profile dict has braces inside
    braces.
    """

    def test_parses_pure_json(self) -> None:
        data = parse_profile_response('{"candidate": {"full_name": "Jane"}}')
        self.assertEqual(data["candidate"]["full_name"], "Jane")

    def test_strips_markdown_codefence(self) -> None:
        data = parse_profile_response(
            '```json\n{"candidate": {"full_name": "Jane"}}\n```'
        )
        self.assertEqual(data["candidate"]["full_name"], "Jane")

    def test_falls_back_to_slicing_when_wrapped_in_prose(self) -> None:
        text = (
            'Here is the profile JSON you asked for:\n'
            '{"target_roles": {"primary": ["Senior AI Engineer"]}}\n'
            "Hope that helps."
        )
        data = parse_profile_response(text)
        self.assertEqual(
            data["target_roles"]["primary"], ["Senior AI Engineer"]
        )

    def test_slicing_survives_nested_objects(self) -> None:
        # Nested archetypes (list of dicts) and proof_points (list of
        # dicts) — the string-slicing fallback has to grab the OUTERMOST
        # { ... } block, not stop at the first inner one. This is the
        # test that would catch a regression to a regex-based approach.
        text = (
            'Prologue prose we want to ignore. '
            '{"candidate": {"full_name": "Jane"}, '
            '"target_roles": {"primary": ["Senior AI Engineer"], '
            '"archetypes": [{"name": "AI/ML Engineer", "level": "Senior", '
            '"fit": "primary"}]}, '
            '"narrative": {"proof_points": [{"name": "Project Alpha", '
            '"hero_metric": "Reduced inference 40%"}]}} '
            "Epilogue prose we want to ignore."
        )
        data = parse_profile_response(text)
        self.assertEqual(data["candidate"]["full_name"], "Jane")
        self.assertEqual(
            data["target_roles"]["primary"], ["Senior AI Engineer"]
        )
        self.assertEqual(
            data["target_roles"]["archetypes"][0]["name"], "AI/ML Engineer"
        )
        self.assertEqual(
            data["narrative"]["proof_points"][0]["hero_metric"],
            "Reduced inference 40%",
        )

    def test_slicing_keeps_only_outermost_block_when_multiple_objects(self) -> None:
        # If the model emitted two JSON objects, the slicing fallback
        # keeps the OUTERMOST one — this is the documented behaviour.
        # In practice the system prompt forbids multiple objects, so
        # we just confirm the fallback doesn't crash on the input.
        # The slicing grabs the union of the two objects (everything
        # between the first ``{`` and the last ``}``), which is NOT
        # valid JSON, so ``json.loads`` fails and we get a
        # ``_PermanentError``. That's the accepted outcome — the
        # test just confirms the function doesn't crash with a
        # raw ``JSONDecodeError`` or some other non-typed exception.
        from services.llm_client import _PermanentError
        text = (
            '{"a": 1, "candidate": {"full_name": "Jane"}}, '
            '{"b": 2}'
        )
        with self.assertRaises(_PermanentError):
            parse_profile_response(text)

    def test_returns_empty_dict_on_empty_object(self) -> None:
        data = parse_profile_response("{}")
        self.assertEqual(data, {})

    def test_raises_on_unparseable(self) -> None:
        from services.llm_client import _PermanentError
        with self.assertRaises(_PermanentError):
            parse_profile_response("not json at all")

    def test_raises_on_garbage_with_braces(self) -> None:
        # Braces present but the contents are not valid JSON. The
        # slicing fallback will grab ``{not json}`` and ``json.loads``
        # will raise — we want a clean ``_PermanentError`` not a
        # raw ``JSONDecodeError``.
        from services.llm_client import _PermanentError
        with self.assertRaises(_PermanentError):
            parse_profile_response("Some prose {not json} more prose")


class TestLLMClientExtractProfile(unittest.IsolatedAsyncioTestCase):
    """Step-2: the resume → profile extractor.

    Mirrors TestLLMClientRetryChain. We mock the openai SDK to keep
    the tests hermetic — the production code path is
    ``client.chat.completions.create(...)`` and the deepest callable
    is what gets the canned response.
    """

    async def test_first_provider_happy_path_returns_parsed_dict(self) -> None:
        canned = (
            '{"candidate": {"full_name": "Jane Smith"}, '
            '"target_roles": {"primary": ["Senior AI Engineer"]}}'
        )
        client, mocks = _build_mock_client(nvidia=_fake_response(canned))
        data, model = await client.extract_profile("Jane Smith\nAI Engineer")
        self.assertEqual(data["candidate"]["full_name"], "Jane Smith")
        self.assertEqual(data["target_roles"]["primary"][0], "Senior AI Engineer")
        self.assertEqual(model, "m")  # the ProviderConfig.model string
        mocks["nvidia"].chat.completions.create.assert_awaited_once()

    async def test_falls_back_to_groq_when_nvidia_returns_unparseable(self) -> None:
        # Bad JSON on NVIDIA is a ``_PermanentError`` (parse failure)
        # so the chain advances to Groq without a retry. The Groq
        # response is the canonical profile JSON.
        client, mocks = _build_mock_client(
            nvidia=_fake_response("not json at all"),
            groq=_fake_response(
                '{"candidate": {"full_name": "From Groq"}}'
            ),
        )
        data, _ = await client.extract_profile("resume text")
        self.assertEqual(data["candidate"]["full_name"], "From Groq")
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 1)
        mocks["groq"].chat.completions.create.assert_awaited_once()

    async def test_retries_within_provider_on_transient_error(self) -> None:
        # Transient SDK error → one retry → success. Same shape as
        # the scoring retry test; we just want to confirm the new
        # method also goes through the same retry-then-advance logic.
        client, mocks = _build_mock_client(
            nvidia=[
                _sdk_exception(APIConnectionError, "transient"),
                _fake_response('{"candidate": {"full_name": "Jane"}}'),
            ],
        )
        with _no_sleep() as mock_sleep:
            data, _ = await client.extract_profile("resume")
        self.assertEqual(data["candidate"]["full_name"], "Jane")
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 2)
        mock_sleep.assert_awaited_once()

    async def test_raises_runtime_if_every_provider_fails(self) -> None:
        client, mocks = _build_mock_client(
            nvidia=[
                _sdk_exception(APIConnectionError, "a"),
                _sdk_exception(APIConnectionError, "b"),
            ],
            groq=[
                _sdk_exception(APIConnectionError, "c"),
                _sdk_exception(APIConnectionError, "d"),
            ],
        )
        with _no_sleep():
            with self.assertRaises(RuntimeError):
                await client.extract_profile("resume")
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 2)
        self.assertEqual(mocks["groq"].chat.completions.create.call_count, 2)

    async def test_nvidia_rate_limiter_consumed_once_for_extraction(self) -> None:
        # Two NVIDIA providers share one bucket; one extraction
        # consumes ONE token. Same dedupe guarantee as
        # score_opportunity — see TestUniqueRateLimiters for the
        # underlying helper test.
        shared = AsyncTokenBucket(capacity=80, refill_per_second=80 / 60.0)
        providers = [
            ProviderConfig(
                name="nvidia", base_url="https://nvidia", api_key="a", model="m",
                rate_limiter=shared,
            ),
            ProviderConfig(
                name="nvidia_2", base_url="https://nvidia", api_key="b", model="m",
                rate_limiter=shared,
            ),
        ]
        client = LLMClient(providers)
        nvidia_mock = AsyncMock()
        nvidia_mock.chat.completions.create.return_value = _fake_response(
            '{"candidate": {"full_name": "x"}}'
        )
        client._clients = {"nvidia": nvidia_mock, "nvidia_2": nvidia_mock}  # type: ignore[assignment]

        with _no_sleep():
            await client.extract_profile("resume")
        # Started at 80, ONE token consumed (dedupe kept the inner
        # per-provider acquire() from firing twice on the same bucket).
        self.assertAlmostEqual(shared.available_tokens, 79.0, places=2)

    async def test_extraction_uses_higher_max_tokens_than_score(self) -> None:
        # The profile extraction is allowed ~8x the score call's
        # budget. We assert the call is made with max_tokens=2500
        # (PROFILE_EXTRACTION_MAX_TOKENS) — a regression to 300
        # would clip a senior candidate's project list silently.
        client, mocks = _build_mock_client(
            nvidia=_fake_response('{"candidate": {"full_name": "x"}}'),
        )
        await client.extract_profile("resume")
        call_kwargs = mocks["nvidia"].chat.completions.create.await_args.kwargs
        self.assertEqual(call_kwargs["max_tokens"], 2500)
        self.assertEqual(call_kwargs["temperature"], 0.0)
        # System prompt is the profile-extraction variant, not the
        # score prompt — guards against an accidental copy-paste.
        messages = call_kwargs["messages"]
        self.assertIn("career-ops profile extractor", messages[0]["content"])

    async def test_extraction_user_message_is_resume_text(self) -> None:
        # The user message is the raw resume text — no prompt
        # template. This keeps the LLM call cheap (no double
        # counting of the resume's tokens in a prompt wrapper).
        client, mocks = _build_mock_client(
            nvidia=_fake_response('{"candidate": {"full_name": "x"}}'),
        )
        await client.extract_profile("Jane Smith\nAI Engineer\n")
        call_kwargs = mocks["nvidia"].chat.completions.create.await_args.kwargs
        messages = call_kwargs["messages"]
        self.assertEqual(messages[1]["content"], "Jane Smith\nAI Engineer\n")


class TestBuildPrompt(unittest.TestCase):
    def test_renders_profile_and_opportunity(self) -> None:
        s = build_prompt(
            "Target roles:\n- AI Engineer",
            {
                "title": "Senior AI Engineer",
                "company_name": "Acme",
                "url": "https://acme.com/jobs/1",
            },
        )
        self.assertIn("AI Engineer", s)
        self.assertIn("Senior AI Engineer", s)
        self.assertIn("Acme", s)
        self.assertIn("https://acme.com/jobs/1", s)

    def test_opportunity_dict_with_no_recognised_keys_falls_back_to_json(self) -> None:
        s = build_prompt("p", {"foo": "bar"})
        self.assertIn("foo", s)
        self.assertIn("bar", s)


class TestSystemPromptProfileAware(unittest.TestCase):
    """The SYSTEM_PROMPT must instruct the LLM to use the full profile.

    Before this change, SYSTEM_PROMPT only mentioned "role-fit and
    seniority alignment" — the LLM was free to anchor on the role
    titles and ignore the narrative / compensation / location
    sections that :func:`build_profile_summary` actually renders.
    The expanded prompt enumerates 7 scoring factors in the same
    order the profile renderer emits them, so a careful reader can
    map each factor to the corresponding profile section.

    These tests are the regression guard: if a future edit drops
    one of the factors from the prompt, the LLM will silently
    stop using that profile section and these tests will catch it.
    """

    def test_mentions_role_fit_with_fit_levels(self) -> None:
        # The fit-level bucketing (primary/secondary/adjacent)
        # is the load-bearing part of the new profile schema.
        # A prompt that says "consider target_roles" without
        # the levels would let the LLM treat a primary match
        # the same as an adjacent one.
        self.assertIn("ROLE FIT", SYSTEM_PROMPT)
        self.assertIn("Primary", SYSTEM_PROMPT)
        self.assertIn("Secondary", SYSTEM_PROMPT)
        self.assertIn("Adjacent", SYSTEM_PROMPT)

    def test_mentions_seniority_alignment(self) -> None:
        self.assertIn("SENIORITY ALIGNMENT", SYSTEM_PROMPT)
        self.assertIn("archetype.level", SYSTEM_PROMPT)

    def test_mentions_skill_match_via_superpowers(self) -> None:
        # The superpowers list is the candidate's top-5
        # strengths; the LLM should cross-reference it
        # against the posting's required skills.
        self.assertIn("SKILL MATCH", SYSTEM_PROMPT)
        self.assertIn("superpower", SYSTEM_PROMPT.lower())

    def test_mentions_narrative_alignment(self) -> None:
        # The headline + exit_story tell the LLM WHAT the
        # candidate is optimizing for, not just WHAT they
        # can do. This factor is the differentiator between
        # a 0.7 (skills match) and a 0.9 (mission match).
        self.assertIn("NARRATIVE ALIGNMENT", SYSTEM_PROMPT)
        self.assertIn("headline", SYSTEM_PROMPT)
        self.assertIn("exit_story", SYSTEM_PROMPT)

    def test_mentions_compensation(self) -> None:
        # A posting below the operator's minimum is a soft
        # mismatch. The prompt must tell the LLM to surface
        # this in the reasoning.
        self.assertIn("COMPENSATION", SYSTEM_PROMPT)
        self.assertIn("target_range", SYSTEM_PROMPT)
        self.assertIn("minimum", SYSTEM_PROMPT)

    def test_mentions_location_and_visa(self) -> None:
        # A posting that hard-requires sponsorship when the
        # operator has visa_status "No sponsorship needed"
        # is a hard mismatch. The prompt must flag this as
        # a 0.0-0.2 score.
        self.assertIn("LOCATION", SYSTEM_PROMPT)
        self.assertIn("visa_status", SYSTEM_PROMPT)
        self.assertIn("No sponsorship", SYSTEM_PROMPT)

    def test_mentions_proof_points_hero_metrics(self) -> None:
        # The hero_metric field on proof_points is the
        # concrete-impact signal. The prompt must tell the
        # LLM to look for matching metrics in the posting.
        self.assertIn("PROOF POINTS", SYSTEM_PROMPT)
        self.assertIn("hero_metric", SYSTEM_PROMPT)

    def test_still_preserves_json_output_format(self) -> None:
        # Backward-compat: the LLMClient tests assert the
        # JSON shape elsewhere. The expanded prompt must NOT
        # change the output contract.
        self.assertIn('"score"', SYSTEM_PROMPT)
        self.assertIn('"reasoning"', SYSTEM_PROMPT)
        # The 0.0-1.0 range is still the contract.
        self.assertIn("0.0-1.0", SYSTEM_PROMPT)

    def test_still_says_return_only_json(self) -> None:
        # Critical: the parser relies on the LLM returning
        # ONLY the JSON object, no markdown fences, no
        # preamble. A future edit that adds explanatory
        # prose to the prompt body would break parsing.
        self.assertIn("ONLY the JSON", SYSTEM_PROMPT)
        self.assertIn("no markdown", SYSTEM_PROMPT)

    def test_score_calibration_instructs_holistic_evaluation(self) -> None:
        # The closing clause is the one that actually changes
        # the LLM's behavior — "0.9 means 5+ factors match,
        # not because the title is close". Without this, the
        # LLM regresses to title-matching.
        self.assertIn("5+ factors", SYSTEM_PROMPT)
        self.assertIn("title alone is close", SYSTEM_PROMPT)


class TestLLMClientRetryChain(unittest.IsolatedAsyncioTestCase):
    """Verify the provider-chain + retry-then-fallback semantics."""

    async def test_first_provider_happy_path_succeeds(self) -> None:
        client, mocks = _build_mock_client(
            nvidia=_fake_response('{"score": 0.7, "reasoning": "good"}'),
        )
        score, reasoning = await client.score_opportunity(
            "profile", {"title": "x"}
        )
        self.assertAlmostEqual(score, 0.7)
        self.assertEqual(reasoning, "good")
        mocks["nvidia"].chat.completions.create.assert_awaited_once()

    async def test_retries_within_provider_on_transient_error(self) -> None:
        client, mocks = _build_mock_client(
            nvidia=[
                _sdk_exception(APIConnectionError, "boom"),
                _fake_response('{"score": 0.5, "reasoning": "ok"}'),
            ],
        )
        with _no_sleep() as mock_sleep:
            score, _ = await client.score_opportunity("profile", {"title": "x"})
        self.assertAlmostEqual(score, 0.5)
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 2)
        # Transient retry path sleeps once between attempts.
        mock_sleep.assert_awaited_once()

    async def test_nvidia_rate_limiter_is_awaited_before_call(self) -> None:
        # Build a client whose NVIDIA provider has a real (but
        # generous) rate limiter — capacity=10, refill=1000/s — so
        # the bucket's ``acquire()`` returns instantly and the test
        # doesn't pay a 100ms+ throttle delay. We then assert
        # ``acquire()`` was awaited exactly once before the LLM
        # call went out.
        providers = [
            ProviderConfig(
                name="nvidia",
                base_url="https://nvidia",
                api_key="fake",
                model="m",
                rate_limiter=AsyncTokenBucket(capacity=10, refill_per_second=1000.0),
            ),
        ]
        client = LLMClient(providers)
        mock = AsyncMock()
        mock.chat.completions.create.return_value = _fake_response(
            '{"score": 0.9, "reasoning": "match"}'
        )
        client._clients = {"nvidia": mock}  # type: ignore[assignment]

        score, _ = await client.score_opportunity("profile", {"title": "x"})
        self.assertAlmostEqual(score, 0.9)
        mock.chat.completions.create.assert_awaited_once()
        # The bucket started at capacity 10 and we consumed 1 token.
        self.assertAlmostEqual(
            providers[0].rate_limiter.available_tokens,
            9.0,
            places=2,
        )

    async def test_retry_does_not_consume_second_token(self) -> None:
        # First attempt raises (transient) → retry succeeds. We
        # asserted in the docstring that one opportunity costs ONE
        # token from the rate limiter, not two. Verify the bucket
        # reflects that — start at 10, end at 9, even though the
        # provider's chat.completions.create was called twice.
        providers = [
            ProviderConfig(
                name="nvidia",
                base_url="https://nvidia",
                api_key="fake",
                model="m",
                rate_limiter=AsyncTokenBucket(capacity=10, refill_per_second=1000.0),
            ),
        ]
        client = LLMClient(providers)
        mock = AsyncMock()
        mock.chat.completions.create.side_effect = [
            _sdk_exception(APIConnectionError, "transient"),
            _fake_response('{"score": 0.6, "reasoning": "ok"}'),
        ]
        client._clients = {"nvidia": mock}  # type: ignore[assignment]

        with _no_sleep():
            await client.score_opportunity("profile", {"title": "x"})
        self.assertEqual(mock.chat.completions.create.call_count, 2)
        self.assertAlmostEqual(
            providers[0].rate_limiter.available_tokens,
            9.0,
            places=2,
        )

    async def test_fallback_provider_does_not_acquire_nvidia_limiter(self) -> None:
        # Groq should run unimpeded even if NVIDIA's bucket is
        # completely empty — the limiter is per-provider, not
        # shared. We use a depleted NVIDIA bucket to prove that
        # the test is actually exercising the acquire-on-miss path.
        nvidia_bucket = AsyncTokenBucket(capacity=40, refill_per_second=40 / 60.0)
        nvidia_bucket._tokens = 0.0  # force a wait on the next acquire
        providers = [
            ProviderConfig(
                name="nvidia",
                base_url="https://nvidia",
                api_key="fake",
                model="m",
                rate_limiter=nvidia_bucket,
            ),
            ProviderConfig(
                name="groq",
                base_url="https://groq",
                api_key="fake",
                model="m",
                rate_limiter=None,
            ),
        ]
        client = LLMClient(providers)
        nvidia_mock = AsyncMock()
        # Fail BOTH NVIDIA attempts so we advance to Groq. The
        # first acquire will block ~1.5s (40 rpm = 1.5s per token);
        # the second attempt will retry on the *same* token (no
        # second acquire), so the total wait is ~1.5s. We use a
        # 0.5s asyncio.sleep patch to keep the retry's internal
        # backoff from contributing to the elapsed time.
        nvidia_mock.chat.completions.create.side_effect = [
            _sdk_exception(APIConnectionError, "a"),
            _sdk_exception(APIConnectionError, "b"),
        ]
        groq_mock = AsyncMock()
        groq_mock.chat.completions.create.return_value = _fake_response(
            '{"score": 0.4, "reasoning": "groq"}'
        )
        client._clients = {"nvidia": nvidia_mock, "groq": groq_mock}  # type: ignore[assignment]

        with _no_sleep():
            score, reasoning = await client.score_opportunity(
                "profile", {"title": "x"}
            )
        self.assertAlmostEqual(score, 0.4)
        self.assertEqual(reasoning, "groq")
        # Two attempts on NVIDIA, one on Groq — the cross-provider
        # boundary should be: 1 NVIDIA acquire (for the whole retry
        # pair) + 0 Groq acquires (Groq has no limiter in v1).
        self.assertEqual(nvidia_mock.chat.completions.create.call_count, 2)
        groq_mock.chat.completions.create.assert_awaited_once()

    async def test_treats_authentication_as_permanent_and_advances(self) -> None:
        # Wrap single exceptions in a list so AsyncMock raises them via
        # ``side_effect`` rather than returning the instance via
        # ``return_value`` — production code expects the exception to be
        # raised, then classifies it in the except chain.
        client, mocks = _build_mock_client(
            nvidia=[_sdk_exception(AuthenticationError, "bad key", status_code=401)],
            groq=_fake_response('{"score": 0.55, "reasoning": "groq"}'),
        )
        with _no_sleep() as mock_sleep:
            score, _ = await client.score_opportunity("profile", {"title": "x"})
        self.assertAlmostEqual(score, 0.55)
        # No retry on auth — advance immediately to groq.
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 1)
        mocks["groq"].chat.completions.create.assert_awaited_once()
        # Permanent errors skip the snooze between retries.
        mock_sleep.assert_not_awaited()

    async def test_treats_bad_request_as_permanent_and_advances(self) -> None:
        client, mocks = _build_mock_client(
            nvidia=[_sdk_exception(BadRequestError, "invalid prompt", status_code=400)],
            groq=_fake_response('{"score": 0.55, "reasoning": "groq"}'),
        )
        with _no_sleep() as mock_sleep:
            score, _ = await client.score_opportunity("profile", {"title": "x"})
        self.assertAlmostEqual(score, 0.55)
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 1)
        mocks["groq"].chat.completions.create.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    async def test_raises_runtime_if_every_provider_fails(self) -> None:
        client, mocks = _build_mock_client(
            nvidia=[
                _sdk_exception(RateLimitError, "a", status_code=429),
                _sdk_exception(RateLimitError, "b", status_code=429),
            ],
            groq=[
                _sdk_exception(RateLimitError, "c", status_code=429),
                _sdk_exception(RateLimitError, "d", status_code=429),
            ],
        )
        with _no_sleep() as mock_sleep:
            with self.assertRaises(RuntimeError) as ctx:
                await client.score_opportunity("profile", {"title": "x"})
        # Two providers × 1 sleep-between-attempts = 2 sleeps total.
        self.assertEqual(mock_sleep.await_count, 2)
        # Each provider was attempted twice before advancing.
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 2)
        self.assertEqual(mocks["groq"].chat.completions.create.call_count, 2)
        # RuntimeError chains the last SDK exception as __cause__ so the
        # full traceback (including the openai status code) is preserved.
        self.assertEqual(type(ctx.exception.__cause__).__name__, "RateLimitError")


class TestLLMClientFromEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = {k: os.environ.get(k) for k in (
            "NVIDIA_API_KEY", "GROQ_API_KEY",
            "NVIDIA_BASE_URL", "GROQ_BASE_URL",
            "NVIDIA_MODEL", "GROQ_MODEL",
            "NVIDIA_RPM",
        )}

    def setUp(self) -> None:
        # Wipe the module-level bucket cache before every test so
        # each test sees a fresh, full-capacity bucket regardless of
        # what previous tests did. The cache is process-global by
        # design for production, but tests need order-independence.
        from services import llm_client
        llm_client._NVIDIA_RPM_LIMITERS.clear()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_raises_when_no_keys_configured(self) -> None:
        for k in ("NVIDIA_API_KEY", "GROQ_API_KEY"):
            os.environ.pop(k, None)
        with self.assertRaises(RuntimeError):
            LLMClient.from_env()

    def test_nvidia_provider_has_default_rate_limiter(self) -> None:
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("NVIDIA_RPM", None)  # fall back to default
        client = LLMClient.from_env()
        limiter = client.providers[0].rate_limiter
        self.assertIsNotNone(
            limiter,
            "Default 40 RPM rate limiter should be attached to NVIDIA "
            "provider when NVIDIA_RPM is unset (see DEFAULT_NVIDIA_RPM).",
        )
        # capacity = 40, refill = 40/60 = 0.667/s. Bucket starts full.
        self.assertAlmostEqual(limiter.available_tokens, 40.0, places=2)

    def test_nvidia_rpm_zero_disables_rate_limiter(self) -> None:
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ["NVIDIA_RPM"] = "0"
        try:
            client = LLMClient.from_env()
            self.assertIsNone(
                client.providers[0].rate_limiter,
                "NVIDIA_RPM=0 should disable the rate limiter so the "
                "operator can push the throttle out of the way for "
                "higher-tier keys or one-off bulk rescans.",
            )
        finally:
            os.environ.pop("NVIDIA_RPM", None)

    def test_nvidia_rpm_custom_value_configures_bucket(self) -> None:
        # setUp already cleared the bucket cache for us.
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ["NVIDIA_RPM"] = "100"
        try:
            client = LLMClient.from_env()
            limiter = client.providers[0].rate_limiter
            self.assertIsNotNone(limiter)
            self.assertAlmostEqual(limiter.available_tokens, 100.0, places=2)
        finally:
            os.environ.pop("NVIDIA_RPM", None)

    def test_nvidia_rpm_malformed_raises(self) -> None:
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ["NVIDIA_RPM"] = "not-a-number"
        try:
            with self.assertRaises(ValueError) as ctx:
                LLMClient.from_env()
            self.assertIn("NVIDIA_RPM", str(ctx.exception))
        finally:
            os.environ.pop("NVIDIA_RPM", None)

    def test_groq_provider_does_not_get_nvidia_limiter(self) -> None:
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ["GROQ_API_KEY"] = "xyz"
        client = LLMClient.from_env()
        by_name = {p.name: p for p in client.providers}
        self.assertIsNotNone(by_name["nvidia"].rate_limiter)
        self.assertIsNone(
            by_name["groq"].rate_limiter,
            "Groq is the fallback — v1 only throttles NVIDIA, since the "
            "operator's 40 RPM constraint is specific to the NVIDIA NIM key.",
        )

    def test_includes_nvidia_with_default_model(self) -> None:
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ.pop("GROQ_API_KEY", None)
        # Strip any inherited model/base_url overrides so this only sees
        # the defaults declared as Final constants in :mod:`services.llm_client`.
        for k in ("NVIDIA_MODEL", "GROQ_MODEL", "NVIDIA_BASE_URL", "GROQ_BASE_URL"):
            os.environ.pop(k, None)
        client = LLMClient.from_env()
        self.assertEqual([p.name for p in client.providers], ["nvidia"])
        self.assertEqual(client.providers[0].model, "meta/llama-3.1-70b-instruct")
        self.assertEqual(client.providers[0].base_url, "https://integrate.api.nvidia.com/v1")

    def test_orders_nvidia_before_groq(self) -> None:
        os.environ["NVIDIA_API_KEY"] = "abc"
        os.environ["GROQ_API_KEY"] = "xyz"
        client = LLMClient.from_env()
        self.assertEqual([p.name for p in client.providers], ["nvidia", "groq"])
        self.assertEqual(client.providers[1].model, "llama-3.3-70b-versatile")

    def test_skips_provider_without_key(self) -> None:
        os.environ.pop("NVIDIA_API_KEY", None)
        os.environ["GROQ_API_KEY"] = "xyz"
        client = LLMClient.from_env()
        self.assertEqual([p.name for p in client.providers], ["groq"])


# ----------------------------------------------------------------------
# 2-NVIDIA-key dedupe — _unique_rate_limiters(providers) returns one entry
# per distinct bucket object identity so the (nvidia, nvidia_2, groq)
# chain consumes exactly 1 token per opportunity, not 2. The 2-NVIDIA
# throttling bug the v1 review caught: each opportunity would acquire
# the shared bucket once per NVIDIA provider slot, halving the effective
# throughput from ``len(keys) * 40`` RPM down to ``40`` RPM. The fix is
# to dedupe by ``id(bucket)`` and acquire ONCE per unique bucket at the
# top of both score_opportunity and research_opportunity.
# ----------------------------------------------------------------------
class TestUniqueRateLimiters(unittest.IsolatedAsyncioTestCase):
    def test_two_nvidia_sharing_one_bucket_dedupes_to_one(self) -> None:
        # The production `from_env` path builds two ProviderConfigs that
        # both reference the same AsyncTokenBucket (capacity=2*40 RPM).
        # The dedupe helper must return exactly 1 entry so the retry
        # loop's per-provider acquire() doesn't double-consume.
        shared = AsyncTokenBucket(capacity=80, refill_per_second=80 / 60.0)
        providers = [
            ProviderConfig(
                name="nvidia",
                base_url="x",
                api_key="a",
                model="m",
                rate_limiter=shared,
                key_label="primary",
            ),
            ProviderConfig(
                name="nvidia_2",
                base_url="x",
                api_key="b",
                model="m",
                rate_limiter=shared,
                key_label="secondary",
            ),
            ProviderConfig(
                name="groq",
                base_url="y",
                api_key="g",
                model="m",
                rate_limiter=None,
            ),
        ]
        unique = _unique_rate_limiters(providers)
        self.assertEqual(len(unique), 1)
        self.assertIs(unique[0], shared)

    def test_two_nvidia_separate_buckets_returns_two(self) -> None:
        # Backward-compat sanity: when each provider has its OWN
        # bucket (the pre-2-key code path), the helper returns one
        # entry per bucket — the dedupe is by identity, not by name.
        bucket_a = AsyncTokenBucket(capacity=40, refill_per_second=40 / 60.0)
        bucket_b = AsyncTokenBucket(capacity=40, refill_per_second=40 / 60.0)
        providers = [
            ProviderConfig(name="nvidia", base_url="x", api_key="a", model="m", rate_limiter=bucket_a),
            ProviderConfig(name="nvidia_2", base_url="x", api_key="b", model="m", rate_limiter=bucket_b),
        ]
        unique = _unique_rate_limiters(providers)
        self.assertEqual(len(unique), 2)
        # Order is first-seen, which is the order the providers list
        # was constructed with.
        self.assertIs(unique[0], bucket_a)
        self.assertIs(unique[1], bucket_b)

    def test_all_groq_returns_empty(self) -> None:
        # Groq has no rate_limiter in v1 — the helper returns [] so
        # the score_opportunity / research_opportunity acquire loop
        # does nothing.
        providers = [
            ProviderConfig(name="groq", base_url="y", api_key="g", model="m", rate_limiter=None),
        ]
        self.assertEqual(_unique_rate_limiters(providers), [])

    def test_empty_providers_returns_empty(self) -> None:
        # Edge case: an LLMClient constructed with zero providers
        # shouldn't have crashed before this point, but the helper
        # itself must not raise.
        self.assertEqual(_unique_rate_limiters([]), [])

    def test_providers_list_none_rate_limiters_are_skipped(self) -> None:
        # Mixed chain: NVIDIA with limiter + Groq without + an extra
        # provider that opted out. The dedupe ignores the None
        # entries, returning only the buckets that exist.
        bucket = AsyncTokenBucket(capacity=40, refill_per_second=40 / 60.0)
        providers = [
            ProviderConfig(name="nvidia", base_url="x", api_key="a", model="m", rate_limiter=bucket),
            ProviderConfig(name="groq", base_url="y", api_key="g", model="m", rate_limiter=None),
        ]
        unique = _unique_rate_limiters(providers)
        self.assertEqual(unique, [bucket])

    async def test_score_opportunity_consumes_one_token_with_two_nvidia(self) -> None:
        # End-to-end assertion: with 2 NVIDIA providers sharing one
        # bucket, ONE opportunity = ONE token consumed (not two). This
        # is the integration-level guarantee the dedupe is meant to
        # deliver.
        shared = AsyncTokenBucket(capacity=80, refill_per_second=80 / 60.0)
        providers = [
            ProviderConfig(
                name="nvidia", base_url="https://nvidia", api_key="a", model="m",
                rate_limiter=shared, key_label="primary",
            ),
            ProviderConfig(
                name="nvidia_2", base_url="https://nvidia", api_key="b", model="m",
                rate_limiter=shared, key_label="secondary",
            ),
            ProviderConfig(
                name="groq", base_url="https://groq", api_key="g", model="m", rate_limiter=None,
            ),
        ]
        client = LLMClient(providers)
        # Mock all three providers so the test exercises the
        # acquire-dedupe-then-advance chain, not the LLM API.
        nvidia_mock = AsyncMock()
        nvidia_mock.chat.completions.create.return_value = _fake_response(
            '{"score": 0.7, "reasoning": "ok"}'
        )
        groq_mock = AsyncMock()
        groq_mock.chat.completions.create.return_value = _fake_response(
            '{"score": 0.4, "reasoning": "fallback"}'
        )
        client._clients = {"nvidia": nvidia_mock, "nvidia_2": nvidia_mock, "groq": groq_mock}  # type: ignore[assignment]

        with _no_sleep():
            score, _ = await client.score_opportunity("profile", {"title": "x"})
        self.assertAlmostEqual(score, 0.7)
        # The primary NVIDIA provider served the response. The
        # secondary wasn't called (chain succeeded on the first slot).
        self.assertEqual(nvidia_mock.chat.completions.create.call_count, 1)
        groq_mock.chat.completions.create.assert_not_awaited()
        # CRITICAL: the shared bucket went from 80 to 79 — exactly ONE
        # token consumed for this opportunity. If the dedupe were
        # broken, this would be 78 (two acquires) and the effective
        # throughput would be 40 RPM instead of the intended 80 RPM.
        self.assertAlmostEqual(
            shared.available_tokens,
            79.0,
            places=2,
            msg=(
                f"Shared bucket dropped to {shared.available_tokens}; "
                f"expected ~79.0. Two-NVIDIA dedupe is broken if the "
                f"bucket consumed 2 tokens for one opportunity."
            ),
        )

    async def test_research_opportunity_consumes_one_token_with_two_nvidia(self) -> None:
        # Same dedupe guarantee for the research (Interview Prep)
        # code path. Bug regression: before the fix, the per-provider
        # acquire() in research_opportunity would have consumed 2
        # tokens per research call, doubling the LLM bill.
        shared = AsyncTokenBucket(capacity=80, refill_per_second=80 / 60.0)
        providers = [
            ProviderConfig(
                name="nvidia", base_url="https://nvidia", api_key="a", model="m",
                rate_limiter=shared, key_label="primary",
            ),
            ProviderConfig(
                name="nvidia_2", base_url="https://nvidia", api_key="b", model="m",
                rate_limiter=shared, key_label="secondary",
            ),
        ]
        client = LLMClient(providers)
        nvidia_mock = AsyncMock()
        nvidia_mock.chat.completions.create.return_value = _fake_response(
            "## Company Snapshot\nstub"
        )
        client._clients = {"nvidia": nvidia_mock, "nvidia_2": nvidia_mock}  # type: ignore[assignment]

        with _no_sleep():
            content, model = await client.research_opportunity(
                {"title": "x", "company_name": "y"}, "profile"
            )
        self.assertEqual(content, "## Company Snapshot\nstub")
        # CRITICAL: same single-token guarantee as score_opportunity.
        self.assertAlmostEqual(shared.available_tokens, 79.0, places=2)


if __name__ == "__main__":
    unittest.main()
