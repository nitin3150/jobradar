# import json
# import logging

# import httpx

# from app.config import settings
# from app.utils.backoff import with_backoff

# logger = logging.getLogger(__name__)

# _http_client: httpx.AsyncClient | None = None


# def _get_http_client() -> httpx.AsyncClient:
#     global _http_client
#     if _http_client is None:
#         _http_client = httpx.AsyncClient(timeout=60.0)
#     return _http_client

# @with_backoff(max_retries=2, base_delay=2.0)
# async def call_llm(prompt: str, max_tokens: int = 1024) -> str:
#     """Call the configured LLM provider and return text response."""
#     provider = settings.llm_provider.lower()

#     if provider == "groq":
#         return await _call_groq(prompt, max_tokens)
#     else:
#         raise ValueError(f"Unknown LLM provider: {provider}")

# async def _call_groq(prompt: str, max_tokens: int) -> str:
#     """Call Groq API (free tier: 30 RPM). OpenAI-compatible REST API."""
#     http = _get_http_client()
#     resp = await http.post(
#         "https://api.groq.com/openai/v1/chat/completions",
#         headers={
#             "Authorization": f"Bearer {settings.groq_api_key}",
#             "Content-Type": "application/json",
#         },
#         json={
#             "model": settings.llm_model,
#             "max_tokens": max_tokens,
#             "messages": [{"role": "user", "content": prompt}],
#         },
#     )
#     resp.raise_for_status()
#     return resp.json()["choices"][0]["message"]["content"]

# async def call_llm_json(prompt: str, max_tokens: int = 1024) -> dict | None:
#     """Call the LLM and parse JSON from the response."""
#     try:
#         text = await call_llm(prompt, max_tokens)
#         # Extract JSON from markdown code blocks if present
#         if "```json" in text:
#             text = text.split("```json")[1].split("```")[0]
#         elif "```" in text:
#             text = text.split("```")[1].split("```")[0]
#         return json.loads(text.strip())
#     except json.JSONDecodeError as e:
#         logger.warning(f"Failed to parse JSON from LLM response: {e}")
#         return None
#     except Exception as e:
#         logger.error(f"LLM call failed: {e}")
#         return None


# # Backwards-compatible aliases used by scorer and outreach modules
# call_claude = call_llm
# call_claude_json = call_llm_json

import json
import logging

from litellm import completion

from app.config import settings
from app.utils.backoff import with_backoff

logger = logging.getLogger(__name__)


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


@with_backoff(max_retries=2, base_delay=2.0)
async def call_llm(prompt: str, max_tokens: int = 1024) -> str:

    providers = [
        settings.llm_provider,
        settings.llm_fallback_provider,
    ]

    last_error = None

    for provider in providers:

        if provider not in PROVIDERS:
            continue

        config = PROVIDERS[provider]

        try:

            response = completion(
                model=config["model"],
                api_key=config["api_key"],
                api_base=config.get("api_base"),
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                max_tokens=max_tokens,
            )

            return response.choices[0].message.content

        except Exception as e:
            logger.warning("%s failed: %s", provider, e)
            last_error = e

    raise last_error


async def call_llm_json(prompt: str, max_tokens: int = 1024):

    try:
        text = await call_llm(prompt, max_tokens)

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        return json.loads(text.strip())

    except Exception as e:
        logger.error(e)
        return None


call_claude = call_llm
call_claude_json = call_llm_json