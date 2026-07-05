from fastapi import APIRouter
from pipeline.graph import scan_pipeline

router = APIRouter()

@router.post("/")
async def run_scan():
    scan_pipeline.invoke()

    return {"message": "True"}