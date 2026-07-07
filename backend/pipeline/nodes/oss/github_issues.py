"""Good-First-Issues tracker (GitHub Search API, optional auth).

Hits ``https://api.github.com/search/issues`` with ``label:"good first issue"``,
``is:open``, ``language:<lang>``, ``sort=updated``, ``per_page=15``.

Rate limits:
- **Anonymous (no ``GITHUB_TOKEN`` env var):** 60 req/IP/hour. The helper
  caches one day-keyed result per language so an hourly scheduler doesn't
  burn the budget.
- **Authenticated (``GITHUB_TOKEN`` set, e.g. a PAT or fine-grained token):**
  5,000 req/hour — bigger budget + user-attached rate-limit policies by
  including ``Authorization: Bearer <token>`` on every call.

The token is read once at module import (so a rotating secret requires a
process restart, matching how the rest of the scheduler treats env).

Returns one opportunity **per repository** after grouping issues. The
``top_issues`` extension field lists the concrete issues (so the UI can
link directly to `owner/repo#123`), and ``reachout_strategy`` peers into
the actual asks via the strategy generator run downstream.
"""
from __future__ import annotations

import functools
import os
from datetime import datetime, timezone
from typing import Any

import httpx

SOURCE = "github_issues"
SEARCH_URL = "https://api.github.com/search/issues"

USER_AGENT = "jobradar-oss-scanner/1.0 (+https://github.org/)"

# Read the optional GitHub PAT at import. ``os.environ.get`` is cheap; we
# capture the value once so the auth header is consistent across the whole
# process and easy to monkeypatch in tests.
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()


@functools.lru_cache(maxsize=32)
def _cached_search(language: str, day_key: str, per_page: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Cache key = (language, UTC day, per_page) so one call per language per day.

    Returns ``({repo_path: [issue, ...]}, {repo_path: last_activity_iso})``.
    """
    params = {
        "q": f'label:"good first issue" is:open language:{language}',
        "sort": "updated",
        "order": "desc",
        "per_page": per_page,
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if _GITHUB_TOKEN:
        # Authenticated requests get the 5,000/hr quota and skip the
        # soft-throttle GitHub applies to anonymous bursts.
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    try:
        response = httpx.get(SEARCH_URL, params=params, headers=headers, timeout=15.0, follow_redirects=True)
        # 403 = rate-limited, 422 = bad query — surface both as empty so the runner
        # still emits trending rows if those succeeded.
        if response.status_code in (403, 429):
            return {}, {}
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}, {}

    repo_to_issues: dict[str, list[dict[str, Any]]] = {}
    repo_last_activity: dict[str, str] = {}
    for item in payload.get("items", []):
        repo_url = item.get("repository_url") or ""
        # repo_url looks like https://api.github.com/repos/owner/repo
        parts = repo_url.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        owner, repo_name = parts[-2], parts[-1]
        repo_path = f"{owner}/{repo_name}"

        issue = {
            "number": item.get("number"),
            "title": item.get("title") or "",
            "url": item.get("html_url") or "",
            "labels": [label.get("name", "") for label in (item.get("labels") or [])],
            "updated_at": item.get("updated_at"),
        }
        repo_to_issues.setdefault(repo_path, []).append(issue)

        updated_at = item.get("updated_at")
        if updated_at:
            existing = repo_last_activity.get(repo_path)
            if not existing or updated_at > existing:
                repo_last_activity[repo_path] = updated_at

    return repo_to_issues, repo_last_activity


def _format_opportunity(
    repo_path: str,
    issues_all: list[dict[str, Any]],
    last_activity: str | None,
    score: float = 0.8,
) -> dict[str, Any]:
    # Surface the top 3 to keep the UI cards tight, but count *all* fetched
    # issues in the description so the user sees the real backlog size.
    top_issues = issues_all[:3]
    total = len(issues_all)
    plural = "good-first-issue" if total == 1 else "good-first-issues"
    return {
        "id": f"github-issues:{repo_path}",
        "source": SOURCE,
        "category": "oss",
        "title": repo_path,
        "organization": repo_path.split("/", 1)[0] if "/" in repo_path else repo_path,
        "url": f"https://github.com/{repo_path}",
        "location": "GitHub",
        "tags": ["oss", "github", "good-first-issue"],
        "description": (
            f"{total} open {plural} on this repo — "
            f"top {len(top_issues)} listed below."
        ),
        "published": last_activity or datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": score,
        "top_issues": top_issues,
        "last_activity": last_activity,
        "primary_language": "",
    }


def scan(limit: int = 25, language: str = "python") -> list[dict[str, Any]]:
    """Fetch good-first-issue rows for ``language`` and emit one opp per repo."""
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repo_to_issues, repo_last_activity = _cached_search(language, day_key, per_page=15)

    out: list[dict[str, Any]] = []
    for repo_path, issues in repo_to_issues.items():
        out.append(
            _format_opportunity(
                repo_path=repo_path,
                issues_all=issues,
                last_activity=repo_last_activity.get(repo_path),
            )
        )
        if len(out) >= limit:
            break
    return out
