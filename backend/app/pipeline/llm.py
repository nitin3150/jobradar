"""Multi-provider LLM wrapper with retry and fallback.

Supported providers (all via raw httpx — no extra SDK needed):
- ollama     (FREE, local) — no API key needed
- gemini     (FREE tier)   — 15 RPM, 1M tokens/day free
- groq       (FREE tier)   — 30 RPM, llama/mixtral
- anthropic  (paid)        — Claude via anthropic SDK
- openrouter (mixed)       — aggregator with some free models
"""

import json
import logging

import httpx

from app.config import settings
from app.utils.backoff import with_backoff

logger = logging.getLogger(__name__)

_anthropic_client = None
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=60.0)
    return _http_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


@with_backoff(max_retries=2, base_delay=2.0)
async def call_llm(prompt: str, max_tokens: int = 1024) -> str:
    """Call the configured LLM provider and return text response."""
    provider = settings.llm_provider.lower()

    if provider == "anthropic":
        return await _call_anthropic(prompt, max_tokens)
    elif provider == "gemini":
        return await _call_gemini(prompt, max_tokens)
    elif provider == "groq":
        return await _call_groq(prompt, max_tokens)
    elif provider == "ollama":
        return await _call_ollama(prompt, max_tokens)
    elif provider == "openrouter":
        return await _call_openrouter(prompt, max_tokens)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


async def _call_anthropic(prompt: str, max_tokens: int) -> str:
    """Call Anthropic Claude API (requires anthropic SDK)."""
    client = _get_anthropic_client()
    response = await client.messages.create(
        model=settings.llm_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def _call_groq(prompt: str, max_tokens: int) -> str:
    """Call Groq API (free tier: 30 RPM). OpenAI-compatible REST API."""
    http = _get_http_client()
    resp = await http.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.llm_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def _call_ollama(prompt: str, max_tokens: int) -> str:
    """Call Ollama local server. OpenAI-compatible REST API, no key needed."""
    http = _get_http_client()
    base_url = settings.llm_base_url.rstrip("/")
    resp = await http.post(
        f"{base_url}/chat/completions",
        json={
            "model": settings.llm_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def _call_gemini(prompt: str, max_tokens: int) -> str:
    """Call Google Gemini API (free tier: 15 RPM, 1M tokens/day)."""
    http = _get_http_client()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.llm_model}:generateContent?key={settings.google_api_key}"
    )
    resp = await http.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def _call_openrouter(prompt: str, max_tokens: int) -> str:
    """Call OpenRouter API. OpenAI-compatible REST API."""
    http = _get_http_client()
    resp = await http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.llm_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ── Convenience wrappers (used by scorer, outreach, etc.) ──


async def call_llm_json(prompt: str, max_tokens: int = 1024) -> dict | None:
    """Call the LLM and parse JSON from the response."""
    try:
        text = await call_llm(prompt, max_tokens)
        # Extract JSON from markdown code blocks if present
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse JSON from LLM response: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None


# Backwards-compatible aliases used by scorer and outreach modules
call_claude = call_llm
call_claude_json = call_llm_json
