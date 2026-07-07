"""GitHub Trending scraper (bs4-based).

Parses the public ``https://github.com/trending/<language>?since=daily``
page with BeautifulSoup. Each top-level ``<article>`` is one trending
repo, exposing an ``<h2>`` link to ``/owner/repo``, a description
``<p>``, the primary language span, stars, and forks.

No GitHub credentials are needed since the trending page is public HTML.
The function returns raw repo dicts normalized into the universal
opportunity model; the runner combines them with good-first-issues and
runs the strategy generator.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

# A repo path on GitHub is exactly two slash-separated slugs of word
# characters (letters, digits, dots, dashes, underscores). Anything else
# (single-segment nav like /trending or multi-segment like
# /python/cpython/stargazers) fails this guard.
_REPO_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$")

# Two-segment GitHub nav paths that look like ``/<word>/<slug>`` but aren't
# actually repos. ``_REPO_PATH_RE`` accepts these on shape alone; the
# denylist below rules them out before the regex-match branch. Every entry
# ends with a trailing slash so a legitimate repo like
# ``/teamwork/awesome-repo`` isn't accidentally matched by ``/team``.
#
# TRADE-OFF: any real repo whose owner handle is one of `sponsors`, `orgs`,
# `users`, `topics`, `explore`, `collections`, `marketplace`, `pricing`,
# `features`, `enterprise`, or `team` will be silently dropped. Treat this
# denylist as a heuristic; if any of those handles ever host real repos,
# swap that entry for the GitHub Search API as the source of truth.
_NAVPATH_PREFIXES = (
    "/sponsors/",
    "/orgs/",
    "/users/",
    "/topics/",
    "/explore/",
    "/collections/",
    "/marketplace/",
    "/pricing/",
    "/features/",
    "/enterprise/",
    "/team/",
)

SOURCE = "github"
URL_TEMPLATE = "https://github.com/trending/{language}?since=daily"

USER_AGENT = "jobradar-oss-scanner/1.0 (+https://github.org/)"

# GitHub trending uses compressed notation on dense lists — "3.2k" / "12.4m" / "62,341".
_STAR_COUNT_RE = re.compile(r"([\d.]+)\s*([kKmM]?)")


def _parse_star_count(text: str | None) -> int:
    """Parse GitHub star/fork counts accepting ``"3.2k"``, ``"12.4m"``, ``"62,341"``."""
    if not text:
        return 0
    cleaned = text.strip().replace(",", "")
    match = _STAR_COUNT_RE.search(cleaned)
    if not match:
        return 0
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return 0
    suffix = match.group(2).lower()
    multiplier = {"k": 1_000, "m": 1_000_000, "": 1}.get(suffix, 1)
    return int(value * multiplier)


def _build_opportunity(
    *,
    repo_path: str,
    description: str,
    language: str,
    stars: int,
    forks: int,
    last_activity: str | None,
    score: float = 0.7,
) -> dict[str, Any]:
    """Build a standardized OSS opportunity row from a GitHub Trending card."""
    repo_name = repo_path.strip("/")
    return {
        "id": f"github:{repo_name}",
        "source": SOURCE,
        "category": "oss",
        "title": repo_name,
        "organization": repo_name.split("/", 1)[0] if "/" in repo_name else repo_name,
        "url": f"https://github.com/{repo_name}",
        "location": "GitHub",
        "tags": ["oss", "github", language.lower()] if language else ["oss", "github"],
        "description": description,
        "published": last_activity or datetime.now(timezone.utc).date().isoformat(),
        "salary": None,
        "status": "review",
        "score": score,
        # Domain-specific extensions (other domains ignore these).
        "stars": stars,
        "forks": forks,
        "primary_language": language,
        "last_activity": last_activity,
    }


def _parse_html(html: str, *, language: str, limit: int) -> list[dict[str, Any]]:
    """Pure helper — given an HTML string, parse out opportunities.

    Public callers go through ``scan``, which fetches and delegates here.
    Splitting fetch from parse keeps the parser unit-testable with a
    static fragment.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Loosen GitHub-trending row selection to plain <article>. GitHub rotates
    # presentation classes (Box-row is gone in the modern DOM); structural
    # gating below keeps only repo rows.
    for article in soup.find_all("article"):
        anchor = article.select_one("h2 a[href^='/']")
        if not anchor or not anchor.get("href"):
            continue
        repo_path = anchor["href"].split("?")[0].strip()
        if repo_path.startswith(_NAVPATH_PREFIXES):
            continue
        if not _REPO_PATH_RE.match(repo_path):
            continue
        if repo_path in seen:
            continue
        seen.add(repo_path)

        # First <p> inside the article is the description; matches the historical
        # ``p.col-9`` selector and any future CSS rename without coupling to it.
        description_el = article.find("p")
        description = description_el.get_text(strip=True) if description_el else ""

        language_el = article.select_one("span[itemprop='programmingLanguage']")
        primary_language = language_el.get_text(strip=True) if language_el else language

        # Reference each stat anchor by its *href purpose*, not class, because
        # GitHub rotates Link--muted across header / footer / sidebar usages.
        # Modern GitHub serves forks at /forks only (was /network/members
        # historically), so we match the current shape.
        stars_anchor = article.select_one("a[href$='/stargazers']")
        forks_anchor = article.select_one("a[href$='/forks']")
        stars = _parse_star_count(stars_anchor.get_text(strip=True)) if stars_anchor else 0
        forks = _parse_star_count(forks_anchor.get_text(strip=True)) if forks_anchor else 0

        items.append(_build_opportunity(
            repo_path=repo_path,
            description=description,
            language=primary_language,
            stars=stars,
            forks=forks,
            last_activity=None,
        ))
        if len(items) >= limit:
            break

    return items


def scan(limit: int = 25, language: str = "python") -> list[dict[str, Any]]:
    """Fetch the daily GitHub Trending page for ``language`` and parse repo cards."""
    url = URL_TEMPLATE.format(language=language)
    try:
        response = httpx.get(
            url,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US"},
        )
        response.raise_for_status()
        html = response.text
    except Exception:
        return []

    return _parse_html(html, language=language, limit=limit)
