"""JobRadar FastAPI entry point.

Environment precedence (highest to lowest):

1. **Process env** — shell exports, ``docker compose env:`` keys, etc. (already
   in ``os.environ`` when the Python interpreter starts).
2. **``backend/.env``** — operator overrides for the standalone path
   (``python main.py`` from this directory).
3. **``<repo-root>/.env``** — the shared baseline used by the full Dockerised
   pipeline.

Both files are loaded with ``override=False`` so the process env always wins,
which lets Docker / shell injected values stay authoritative. Backend-level
``.env`` overrides root-level ``.env`` on key conflicts because it's loaded
first.

The loader runs **before** any other import. Several modules
(``pipeline.nodes.oss.github_issues._cached_search`` reads ``GITHUB_TOKEN``
at import time, for example) bind env vars to module-level constants, so the
order matters: ``load_env_files()`` then ``from routes.scanner import …``.

The helper is extracted to ``_load_env_files`` so unit tests can drive it
against a tempdir + tmp ``.env`` file without spawning a subprocess.
"""
import os
from pathlib import Path

from dotenv import load_dotenv


def _load_env_files(
    *,
    backend_dir: Path | None = None,
    repo_root: Path | None = None,
    backend_filename: str = ".env",
    root_filename: str = ".env",
    override: bool = False,
) -> tuple[Path | None, Path | None]:
    """Load ``backend/.env`` then ``<repo_root>/.env`` with the documented precedence.

    Returns a ``(backend_path_loaded, root_path_loaded)`` tuple so tests and
    operators can confirm what was actually picked up. ``None`` for any path
    that didn't exist.

    """
    here = Path(__file__).resolve().parent
    backend_dir = backend_dir or here
    repo_root = repo_root or here.parent

    backend_path = backend_dir / backend_filename
    root_path = repo_root / root_filename

    backend_loaded = load_dotenv(backend_path, override=override) if backend_path.is_file() else None
    root_loaded = load_dotenv(root_path, override=override) if root_path.is_file() else None

    return (backend_path if backend_loaded else None, root_path if root_loaded else None)


# Run the loader BEFORE importing routes/services so env bindings at import
# time (e.g. ``GITHUB_TOKEN`` in ``pipeline.nodes.oss.github_issues``) pick
# up the loaded values.
_BACKEND_ENV, _ROOT_ENV = _load_env_files()

import logging  # noqa: E402  (intentionally after _load_env_files)
import uvicorn  # noqa: E402  (intentionally after _load_env_files)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from routes.dashboard import router as dashboard_router  # noqa: E402
from routes.scanner import router as scanner  # noqa: E402
from routes.outreach import router as outreach_router  # noqa: E402
from routes.companies import router as companies_router  # noqa: E402
from routes.pipeline import router as pipeline_router  # noqa: E402
from routes.applications import router as applications_router  # noqa: E402
from routes.jobs import router as jobs_router  # noqa: E402
from routes.qa_bank import router as qa_bank_router  # noqa: E402
from routes.resumes import router as resumes_router  # noqa: E402
from routes.settings import router as settings_router  # noqa: E402
from utils.logging import RequestLoggingMiddleware, jobradar_lifespan, new_request_id  # noqa: E402

# Wire the logging lifespan into the FastAPI app so every test (which
# constructs a TestClient) and every uvicorn boot runs ``setup_logging``
# + ``dump_routes`` *before* the first request lands. ``lifespan`` is
# the modern replacement for ``@app.on_event("startup")`` and is what
# FastAPI's TestClient exercises on construction.
app = FastAPI(title="JobRadar", lifespan=jobradar_lifespan)

# CORS — the local Vite dev server defaults (port 3000) are always
# allowed. Additional origins can be added at deploy time via the
# ``ALLOWED_ORIGINS`` env var (comma-separated). This keeps the same
# image working for localhost dev, Render production, and any other
# host the operator adds without code changes.
_default_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
_extra_origins = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
_allow_origins = _default_origins + _extra_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Request logging is added LAST so it wraps every other middleware as the
# outermost layer — every response, including preflight rejections and
# downstream 4xx, is recorded once with a single X-Request-ID.
app.add_middleware(RequestLoggingMiddleware)


# ---------------------------------------------------------------------------
# Catch-all 500 handler. FastAPI's HTTPException (4xx) is *not* caught here;
# those are intentional client-visible errors and pass through untouched so
# OpenAPI docs stay accurate. Only uncaught exceptions raised inside a route
# body end up here — we log the full stack trace and return a minimal 500
# with the request_id so operators can grep their logs by that ID.
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Pull request_id from ``request.state`` (set by RequestLoggingMiddleware)
    # and fall back to a fresh one in case the middleware never ran (e.g.
    # an exception fired from a startup hook). We attach the same id to the
    # response header here because ``BaseHTTPMiddleware`` does not always
    # thread Starlette's exception-handler 500 response back through its own
    # success path — so the middleware's ``X-Request-ID`` injection can miss
    # 5xx bodies. Setting it on the JSONResponse directly closes that gap.
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    request.state.request_id = request_id
    logging.getLogger("jobradar.error").exception(
        "UNCAUGHT req_id=%s %s %s -> %s",
        request_id,
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    response = JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": request_id},
    )
    response.headers["X-Request-ID"] = request_id
    return response


# Mount the routers under ``/api/*`` so they line up with ``frontend/src/api/client.js``
# (``baseURL = ${VITE_API_URL}/api``) and the docker-compose ``VITE_API_URL`` shape.
# ``/health`` deliberately stays at the root — it is a well-known liveness path that
# load balancers / uptime probes already expect at ``/health``, not ``/api/health``.
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(scanner, prefix="/api/scan", tags=["Scan jobs"])
app.include_router(outreach_router, prefix="/api/outreach", tags=["Outreach"])
app.include_router(companies_router, prefix="/api/companies", tags=["Companies"])
app.include_router(pipeline_router, prefix="/api/pipeline", tags=["Pipeline"])
app.include_router(applications_router, prefix="/api/applications", tags=["Applications"])
app.include_router(jobs_router, prefix="/api/jobs", tags=["Jobs"])
app.include_router(qa_bank_router, prefix="/api/qa-bank", tags=["QA bank"])
app.include_router(resumes_router, prefix="/api/resumes", tags=["Resumes"])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"])


@app.get("/health")
def root() -> dict[str, str]:
    return {"Message": "Server is running!!"}


def _env_summary() -> dict[str, bool]:
    """Diagnostic endpoint — ``True`` when ``GITHUB_TOKEN`` is loaded, else ``False``.

    Trims the response to a single boolean so the unauthenticated route
    can't leak ``backend/.env`` / repo-root ``.env`` paths or any other
    operational detail. Operators who want richer diagnostics should
    check the server logs (``uvicorn --reload`` ones print a startup
    banner) or the env-var specific tests in ``tests/test_dotenv_loading.py``.
    """
    return {"github_token_set": bool(os.environ.get("GITHUB_TOKEN", "").strip())}


app.add_api_route("/health/env", _env_summary, methods=["GET"], tags=["Health"])


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
