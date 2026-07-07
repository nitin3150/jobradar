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

import uvicorn  # noqa: E402  (intentionally after _load_env_files)
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from routes.dashboard import router as dashboard_router  # noqa: E402
from routes.scanner import router as scanner  # noqa: E402
from routes.outreach import router as outreach_router  # noqa: E402
from routes.companies import router as companies_router  # noqa: E402
from routes.pipeline import router as pipeline_router  # noqa: E402

app = FastAPI(title="JobRadar")

# Permissive CORS for the local Vite dev server on port 3000 (frontend/src/api/client.js
# defaults to ``import.meta.env.VITE_API_URL`` which is ``http://localhost:8000/api`` in dev).
# Allow ``http://localhost:3000`` so cross-origin requests during ``npm run dev`` succeed,
# and 127.0.0.1 as a convenience for curl-from-host debugging. Real production deployments
# should swap this to the deployed frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Mount the routers under ``/api/*`` so they line up with ``frontend/src/api/client.js``
# (``baseURL = ${VITE_API_URL}/api``) and the docker-compose ``VITE_API_URL`` shape.
# ``/health`` deliberately stays at the root — it is a well-known liveness path that
# load balancers / uptime probes already expect at ``/health``, not ``/api/health``.
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(scanner, prefix="/api/scan", tags=["Scan jobs"])
app.include_router(outreach_router, prefix="/api/outreach", tags=["Outreach"])
app.include_router(companies_router, prefix="/api/companies", tags=["Companies"])
app.include_router(pipeline_router, prefix="/api/pipeline", tags=["Pipeline"])


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
