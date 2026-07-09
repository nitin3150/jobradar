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
    AsyncTokenBucket,
    LLMClient,
    ProviderConfig,
    build_prompt,
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
        self.assertAlmostEqual(bucket.available_tokens, 2.0, places=3)
        await bucket.acquire()  # token 2/3
        self.assertAlmostEqual(bucket.available_tokens, 1.0, places=3)
        await bucket.acquire()  # token 3/3
        self.assertAlmostEqual(bucket.available_tokens, 0.0, places=3)

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
        self.assertAlmostEqual(
            bucket._tokens,
            tokens_before,
            places=6,
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


if __name__ == "__main__":
    unittest.main()
