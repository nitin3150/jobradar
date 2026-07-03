"""Application tracking endpoints."""
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.application import Application
from app.models.job import Job
from app.models.company import Company
from app.schemas.application import (
    ApplicationListResponse,
    ApplicationResponse,
    ApplicationStatusUpdate,
)

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("", response_model=ApplicationListResponse)
async def list_applications(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    base_query = (
        select(Application)
        .join(Job, Application.job_id == Job.id)
        .join(Company, Job.company_id == Company.id)
    )
    if status:
        base_query = base_query.where(Application.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    result = await db.execute(
        base_query.order_by(Application.submitted_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .add_columns(Job.title.label("job_title"), Company.name.label("company_name"))
    )
    rows = result.all()

    applications = []
    for row in rows:
        app_obj = row[0]
        job_title = row[1]
        company_name = row[2]
        r = ApplicationResponse.model_validate(app_obj)
        r.job_title = job_title
        r.company_name = company_name
        applications.append(r)

    return ApplicationListResponse(
        applications=applications,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 1,
    )


@router.patch("/{application_id}/status", response_model=ApplicationResponse)
async def update_application_status(
    application_id: UUID,
    update: ApplicationStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    app.status = update.status
    if update.notes is not None:
        app.notes = update.notes
    await db.commit()
    await db.refresh(app)
    return ApplicationResponse.model_validate(app)
