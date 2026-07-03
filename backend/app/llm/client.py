# backend/app/llm/client.py
"""LiteLLM wrapper — single call pattern for all LLM interactions."""

import os

from litellm import completion

from app.config import settings


def _provider_key() -> str:
    """Return the provider-specific API key from settings."""
    provider = settings.llm_provider.lower()
    return {
        "groq": settings.groq_api_key,
        "anthropic": settings.anthropic_api_key,
        "gemini": settings.google_api_key,
        "openrouter": settings.openrouter_api_key,
        "nvidia_nim": settings.nvidia_api_key,
    }.get(provider, "")


def _build_model_string() -> str:
    """Build the model string for LiteLLM routing."""
    model = settings.llm_model
    provider = settings.llm_provider

    # If model already starts with the provider prefix, use as-is
    if model.startswith(f"{provider}/"):
        return model

    # Also accept fully-qualified strings from other providers (e.g. "groq/llama-3.3-70b")
    known_providers = {"groq", "anthropic", "gemini", "openrouter", "nvidia_nim", "ollama", "openai"}
    if "/" in model and model.split("/")[0] in known_providers:
        return model

    return f"{provider}/{model}"


def llm_complete(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str:
    """Call LLM via LiteLLM. Returns content string.

    Args:
        messages: OpenAI-format message list
        model: Override model string (e.g. "groq/llama-3.3-70b-versatile").
               Defaults to settings.llm_provider/settings.llm_model.
        temperature: Sampling temperature.
        max_tokens: Max response tokens.
    """
    resolved_model = model or _build_model_string()

    kwargs: dict = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Prefer explicit LLM_API_KEY; fall back to provider-specific key from settings
    api_key = settings.llm_api_key or _provider_key()
    if api_key:
        kwargs["api_key"] = api_key
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base

    response = completion(**kwargs)
    return response.choices[0].message.content
