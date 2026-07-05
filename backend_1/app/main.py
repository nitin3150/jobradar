import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

    # Start scheduler (read persisted fetch interval from redis)
    from app.scheduler import get_fetch_interval, start_scheduler

    fetch_interval = await get_fetch_interval(app.state.redis)
    start_scheduler(app, fetch_interval_hours=fetch_interval)

    yield

    # Shutdown
    logger.info("Shutting down JobRadar...")
    app.state.scheduler.shutdown(wait=False)
    await app.state.http_client.aclose()
    await close_redis()
    if app.state.browser:
        await app.state.browser.close()
    if app.state.playwright:
        await app.state.playwright.stop()


app = FastAPI(
    title="JobRadar",
    description="Job tracker",
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

_screenshots_dir = Path(settings.apply_worker_screenshot_dir)
_screenshots_dir.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(_screenshots_dir)), name="screenshots")

# Resumes are intentionally NOT mounted as StaticFiles — downloads go through
# the /api/resumes/{id}/download endpoint with Content-Disposition: attachment.

# Register API routes
from app.api.router import api_router

app.include_router(api_router, prefix="/api")