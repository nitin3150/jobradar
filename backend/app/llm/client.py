# backend/app/llm/client.py
"""LiteLLM wrapper — single call pattern for all LLM interactions.

Mirrors the provider/fallback config in app.pipeline.llm: a primary provider
(settings.llm_provider) with a distinct fallback (settings.llm_fallback_provider).
Each provider carries its own model + key + optional api_base.
"""

import logging

from litellm import completion

from app.config import settings

logger = logging.getLogger(__name__)


# Provider -> LiteLLM call config. Keys read from settings at import time.
PROVIDERS = {
    "nvidia": {
        "model": settings.nvidia_model,
        "api_key": settings.nvidia_api_key,
        "api_base": settings.nvidia_base_url,
    },
    "groq": {
        "model": f"groq/{settings.groq_model}",
        "api_key": settings.groq_api_key,
    },
}


def _call(config: dict, messages, temperature, max_tokens, model: str | None = None) -> str:
    """Single LiteLLM completion for one provider config."""
    kwargs: dict = {
        "model": model or config["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if config.get("api_key"):
        kwargs["api_key"] = config["api_key"]
    if config.get("api_base"):
        kwargs["api_base"] = config["api_base"]

    response = completion(**kwargs)
    return response.choices[0].message.content


def llm_complete(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str:
    """Call LLM via LiteLLM. Returns content string.

    Tries the primary provider (settings.llm_provider), then the fallback
    (settings.llm_fallback_provider) if the primary call raises. Unknown
    providers are skipped.

    Args:
        messages: OpenAI-format message list.
        model: Override model string; used with the primary provider's key/base.
        temperature: Sampling temperature.
        max_tokens: Max response tokens.
    """
    providers = [settings.llm_provider, settings.llm_fallback_provider]

    last_error: Exception | None = None
    for name in providers:
        config = PROVIDERS.get(name)
        if not config:
            continue
        try:
            return _call(config, messages, temperature, max_tokens, model=model)
        except Exception as e:  # noqa: BLE001 — try fallback on any provider error
            logger.warning("LLM provider %s failed: %s", name, e)
            last_error = e
            # An explicit model override is provider-specific; don't retry it
            # against a different provider's endpoint.
            if model:
                break

    raise last_error or RuntimeError("No usable LLM provider configured")
