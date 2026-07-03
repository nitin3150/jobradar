"""API router aggregating all sub-routers."""

from fastapi import APIRouter

from app.api.applications import router as applications_router
from app.api.companies import router as companies_router
from app.api.jobs import router as jobs_router
from app.api.outreach import router as outreach_router
from app.api.pipeline import router as pipeline_router
from app.api.qa_bank import router as qa_bank_router

api_router = APIRouter()
api_router.include_router(companies_router)
api_router.include_router(outreach_router)
api_router.include_router(pipeline_router)
api_router.include_router(jobs_router)
api_router.include_router(applications_router)
api_router.include_router(qa_bank_router)
