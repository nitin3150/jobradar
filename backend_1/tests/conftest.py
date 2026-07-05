import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_http_client():
    """Mock httpx.AsyncClient for scraper tests."""
    client = AsyncMock()
    return client


@pytest.fixture
def mock_redis():
    """Mock Redis client for dedup tests."""
    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=0)
    redis.setex = AsyncMock()
    return redis


@pytest.fixture
def mock_settings():
    """Mock settings with all scrapers enabled."""
    from app.config import Settings

    return Settings(
        anthropic_api_key="test-key",
        scraper_sec_edgar_enabled=True,
        scraper_yc_enabled=True,
        scraper_vc_portfolio_enabled=False,
        scraper_twitter_enabled=False,
        scraper_crunchbase_enabled=False,
    )
