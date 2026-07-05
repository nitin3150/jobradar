import uvicorn
from fastapi import FastAPI

from routes.dashboard import router as dashboard_router
from routes.scanner import router as scanner

app = FastAPI(title="JobRadar")

app.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(scanner, prefix="/scan", tags=["Scan jobs"])


@app.get("/health")
def root() -> dict[str, str]:
    return {"Message": "Server is running!!"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
