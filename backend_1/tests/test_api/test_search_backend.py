"""Test the discovery_search_backend='auto' resolver in `search_urls`.

"auto" should pick `serper` when SERPER_API_KEY is set and `playwright`
otherwise. Pinning this avoids regressing the user-friendly opt-in behavior
where setting one env var flips from CAPTCHA-plagued headless search to the
reliable Serper API.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.scrapers.jobs import search as search_module


@pytest.mark.asyncio
async def test_auto_resolves_to_serper_when_key_is_present() -> None:
    """With a non-empty serper_key, "auto" must NOT call the playwright path."""
    fake_http = AsyncMock()
    with patch.object(
        search_module, "_search_serper", new=AsyncMock(return_value=["https://x"])
    ) as m_serper, patch.object(
        search_module, "_search_playwright", new=AsyncMock(return_value=["https://y"])
    ) as m_pw:
        result = await search_module.search_urls(
            '"AI Engineer" site:jobs.ashbyhq.com',
            freshness_hours=24,
            backend="auto",
            browser=AsyncMock(),
            http_client=fake_http,
            serper_key="abc123",
            max_results=10,
        )
    assert result == ["https://x"]
    m_serper.assert_awaited_once()
    m_pw.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_resolves_to_playwright_when_key_is_empty() -> None:
    """With an empty serper_key, "auto" must fall back to the playwright path."""
    fake_http = AsyncMock()
    with patch.object(
        search_module, "_search_serper", new=AsyncMock(return_value=["https://x"])
    ) as m_serper, patch.object(
        search_module, "_search_playwright", new=AsyncMock(return_value=["https://y"])
    ) as m_pw:
        result = await search_module.search_urls(
            '"AI Engineer" site:jobs.ashbyhq.com',
            freshness_hours=24,
            backend="auto",
            browser=AsyncMock(),
            http_client=fake_http,
            serper_key="",
            max_results=10,
        )
    assert result == ["https://y"]
    m_pw.assert_awaited_once()
    m_serper.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_backend_overrides_auto_logic() -> None:
    """Forcing backend='serper' must call serper even when serper_key is empty.

    The "_search_serper" function returns [] with a warning when the key is
    empty, but `search_urls` itself should not silently swap backends — the
    caller asked for serper explicitly.
    """
    with patch.object(
        search_module, "_search_serper", new=AsyncMock(return_value=[])
    ) as m_serper, patch.object(
        search_module, "_search_playwright", new=AsyncMock(return_value=["https://y"])
    ) as m_pw:
        result = await search_module.search_urls(
            "q",
            freshness_hours=24,
            backend="serper",
            browser=AsyncMock(),
            http_client=AsyncMock(),
            serper_key="",
            max_results=10,
        )
    assert result == []
    m_serper.assert_awaited_once()
    m_pw.assert_not_awaited()
