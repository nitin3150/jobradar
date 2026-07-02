# backend/app/llm/client.py
"""LiteLLM wrapper — single call pattern for all LLM interactions."""

import os

from litellm import completion

from app.config import settings


def _build_model_string() -> str:
    """Build the model string for LiteLLM routing."""
    model = settings.llm_model
    provider = settings.llm_provider

    # If model already contains provider prefix (e.g. "groq/llama-3.3-70b"), use as-is
    if "/" in model:
        return model

    # Otherwise prefix with provider
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

    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base

    response = completion(**kwargs)
    return response.choices[0].message.content
