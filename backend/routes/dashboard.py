from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def get_dashboard() -> dict[str, str]:
    return {"message": "this is the dash"}