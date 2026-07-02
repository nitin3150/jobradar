import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.redis_client import close_redis, init_redis

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting FundingRadar...")
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0), follow_redirects=True
    )
    app.state.redis = await init_redis()

    # Playwright browser (only if VC scraper enabled)
    if settings.scraper_vc_portfolio_enabled:
        try:
            from playwright.async_api import async_playwright

            app.state.playwright = await async_playwright().start()
            app.state.browser = await app.state.playwright.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage"]
            )
            logger.info("Playwright browser launched")
        except Exception as e:
            logger.warning(f"Failed to launch Playwright: {e}")
            app.state.playwright = None
            app.state.browser = None
    else:
        app.state.playwright = None
        app.state.browser = None

    # Start scheduler
    from app.scheduler import start_scheduler

    start_scheduler(app)

    yield

    # Shutdown
    logger.info("Shutting down FundingRadar...")
    await app.state.http_client.aclose()
    await close_redis()
    if app.state.browser:
        await app.state.browser.close()
    if app.state.playwright:
        await app.state.playwright.stop()


app = FastAPI(
    title="FundingRadar",
    description="Startup funding intelligence dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routes
from app.api.router import api_router  # noqa: E402

app.include_router(api_router, prefix="/api")
