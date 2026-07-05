"""Company CRUD endpoints."""

import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.schemas.company import (
    CompanyListResponse,
    CompanyResponse,
    CompanyStatusUpdate,
    PipelineStats,
)

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=CompanyListResponse)
async def list_companies(
    category: str | None = None,
    stage: str | None = None,
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    status: str | None = None,
    source: str | None = None,
    role_keyword: str | None = None,
    days_ago: int | None = Query(None, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List companies with filters, sorted by hiring_intent_score DESC."""
    query = select(Company)

    # Apply filters
    if category:
        query = query.where(Company.category == category)
    if stage:
        query = query.where(Company.funding_stage == stage)
    if min_score is not None:
        query = query.where(Company.hiring_intent_score >= min_score)
    if max_score is not None:
        query = query.where(Company.hiring_intent_score <= max_score)
    if status:
        query = query.where(Company.status == status)
    if source:
        query = query.where(Company.source == source)
    if role_keyword:
        # Search in likely_roles JSONB array
        query = query.where(
            Company.likely_roles.cast(str).ilike(f"%{role_keyword}%")
        )
    if days_ago:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_ago)
        query = query.where(Company.created_at >= cutoff)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Sort and paginate
    query = query.order_by(Company.hiring_intent_score.desc())
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    companies = result.scalars().all()

    return CompanyListResponse(
        companies=[CompanyResponse.model_validate(c) for c in companies],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total > 0 else 0,
    )


@router.get("/stats", response_model=PipelineStats)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Get aggregate dashboard stats."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    total = (await db.execute(select(func.count(Company.id)))).scalar() or 0
    new_today = (
        await db.execute(
            select(func.count(Company.id)).where(Company.created_at >= today_start)
        )
    ).scalar() or 0
    high_intent = (
        await db.execute(
            select(func.count(Company.id)).where(Company.hiring_intent_score > 70)
        )
    ).scalar() or 0
    contacted = (
        await db.execute(
            select(func.count(Company.id)).where(Company.status == "contacted")
        )
    ).scalar() or 0

    ngo_count = (
        await db.execute(
            select(func.count(Company.id)).where(Company.category == "ngo")
        )
    ).scalar() or 0

    return PipelineStats(
        total_companies=total,
        new_today=new_today,
        high_intent=high_intent,
        contacted=contacted,
        ngo_count=ngo_count,
    )


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Get a single company by ID."""
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return CompanyResponse.model_validate(company)


@router.patch("/{company_id}/status", response_model=CompanyResponse)
async def update_status(
    company_id: UUID,
    body: CompanyStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a company's outreach status."""
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    company.status = body.status
    company.updated_at = datetime.now(timezone.utc)
    await db.flush()

    return CompanyResponse.model_validate(company)
