"""Job listing endpoints."""
import json
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job, JobStatus
from app.models.company import Company
from app.schemas.job import JobListResponse, JobResponse, JobStatusUpdate

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    # Count with optional status filter
    count_q = select(func.count(Job.id))
    if status:
        count_q = count_q.where(Job.status == status)
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()

    # Main query with join for company name
    query = select(Job, Company.name.label("company_name")).join(
        Company, Job.company_id == Company.id
    )
    if status:
        query = query.where(Job.status == status)
    query = query.order_by(Job.scraped_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    rows = result.all()

    jobs = []
    for row in rows:
        job = row[0]
        company_name = row[1]
        r = JobResponse.model_validate(job)
        r.company_name = company_name
        jobs.append(r)

    return JobListResponse(
        jobs=jobs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total else 1,
    )


@router.get("/pending-count")
async def get_pending_count(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(func.count()).where(Job.status == JobStatus.IN_REVIEW.value)
    )
    return {"count": result.scalar_one()}


@router.patch("/{job_id}/status", response_model=JobResponse)
async def update_job_status(
    job_id: UUID,
    update: JobStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = update.status
    await db.commit()
    await db.refresh(job)
    return JobResponse.model_validate(job)


@router.post("/{job_id}/approve")
async def approve_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Approve job for application — enqueues to apply_queue."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.APPROVED.value
    await db.commit()

    from app.redis_client import get_redis_async
    redis = await get_redis_async()
    await redis.rpush("apply_queue", json.dumps({"job_id": str(job_id)}))

    return {"status": "queued", "job_id": str(job_id)}


@router.post("/{job_id}/reject")
async def reject_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = JobStatus.REJECTED.value
    await db.commit()
    return {"status": "rejected", "job_id": str(job_id)}
