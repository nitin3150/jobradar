"""API router aggregating all sub-routers."""

from fastapi import APIRouter

from app.api.companies import router as companies_router
from app.api.outreach import router as outreach_router
from app.api.pipeline import router as pipeline_router

api_router = APIRouter()
api_router.include_router(companies_router)
api_router.include_router(outreach_router)
api_router.include_router(pipeline_router)
