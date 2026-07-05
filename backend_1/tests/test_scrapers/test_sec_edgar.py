"""Tests for SEC EDGAR scraper."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.scrapers.sec_edgar import SECEdgarScraper


@pytest.fixture
def edgar_response():
    return {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "display_names": ["Test Startup Inc"],
                        "entity_name": "Test Startup Inc",
                        "file_date": "2026-03-27",
                        "total_offering_amount": "5000000",
                        "file_num": "021-12345",
                    }
                },
                {
                    "_source": {
                        "display_names": ["Another Corp"],
                        "entity_name": "Another Corp",
                        "file_date": "2026-03-26",
                        "total_offering_amount": "500000",
                    }
                },
            ]
        }
    }


@pytest.mark.asyncio
async def test_sec_edgar_scraper_parses_results(
    mock_http_client, mock_redis, mock_settings, edgar_response
):
    """Test that SEC EDGAR scraper correctly parses API response."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = edgar_response
    mock_resp.raise_for_status = MagicMock()
    mock_http_client.get = AsyncMock(return_value=mock_resp)

    scraper = SECEdgarScraper(mock_http_client, mock_redis, mock_settings)
    companies = await scraper.scrape()

    assert len(companies) == 2
    assert companies[0]["name"] == "Test Startup Inc"
    assert companies[0]["funding_amount"] == 5_000_000.0
    assert companies[0]["funding_stage"] == "series-a"  # 5M falls in series-a bucket
    assert companies[0]["source"] == "sec_edgar"

    assert companies[1]["name"] == "Another Corp"
    assert companies[1]["funding_amount"] == 500_000.0
    assert companies[1]["funding_stage"] == "pre-seed"


@pytest.mark.asyncio
async def test_sec_edgar_skips_duplicates(
    mock_http_client, mock_redis, mock_settings, edgar_response
):
    """Test that duplicates are skipped via Redis."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = edgar_response
    mock_resp.raise_for_status = MagicMock()
    mock_http_client.get = AsyncMock(return_value=mock_resp)

    # Mark first company as already seen
    mock_redis.exists = AsyncMock(side_effect=[1, 0])

    scraper = SECEdgarScraper(mock_http_client, mock_redis, mock_settings)
    companies = await scraper.scrape()

    assert len(companies) == 1
    assert companies[0]["name"] == "Another Corp"


@pytest.mark.asyncio
async def test_sec_edgar_disabled(mock_http_client, mock_redis, mock_settings):
    """Test that disabled scraper returns empty list."""
    mock_settings.scraper_sec_edgar_enabled = False
    scraper = SECEdgarScraper(mock_http_client, mock_redis, mock_settings)
    result = await scraper.safe_scrape()
    assert result == []
