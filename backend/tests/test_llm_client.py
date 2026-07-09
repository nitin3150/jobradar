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

    async def test_advances_to_fallback_after_persistent_transient(self) -> None:
        client, mocks = _build_mock_client(
            nvidia=[
                _sdk_exception(APITimeoutError, "t1"),
                _sdk_exception(APITimeoutError, "t2"),
            ],
            groq=_fake_response('{"score": 0.42, "reasoning": "groq"}'),
        )
        with _no_sleep() as mock_sleep:
            score, reasoning = await client.score_opportunity("profile", {"title": "x"})
        self.assertAlmostEqual(score, 0.42)
        self.assertEqual(reasoning, "groq")
        self.assertEqual(mocks["nvidia"].chat.completions.create.call_count, 2)
        mocks["groq"].chat.completions.create.assert_awaited_once()
        # One sleep between the two NVIDIA attempts; advances without sleeping again.
        mock_sleep.assert_awaited_once()

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
        )}

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
