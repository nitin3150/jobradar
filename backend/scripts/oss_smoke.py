"""One-shot live validation of the OSS scraper against real GitHub.

Invokes scan_oss() in-process against:
  - https://github.com/trending/python|typescript|go (bs4 HTML)
  - https://api.github.com/search/issues (label:"good first issue")

Verifies the bs4 selectors match the live HTML and the GFI search
returns real data, then summarises with sample rows.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve the backend root relative to this file so the script works no
# matter what cwd the invoker uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.nodes.oss.runner import scan_oss  # noqa: E402
from pipeline.nodes.oss import github_issues as _github_issues  # noqa: E402


def _safe(opp: dict, *keys, default=None):
    for k in keys:
        v = opp.get(k)
        if v is not None:
            return v
    return default


def main() -> None:
    started = datetime.now(timezone.utc)
    opportunities = scan_oss(
        delta_hours=24 * 30,            # wide window so we see data even on slow days
        limit=15,
        sources=["github_trending", "github_issues"],
        languages=["python", "typescript", "go"],
    )
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    # Stats
    total = len(opportunities)
    trending_rows = [o for o in opportunities if o.get("source") == "github"]
    gfi_rows = [o for o in opportunities if o.get("source") == "github_issues"]
    with_stars = sum(1 for o in trending_rows if (o.get("stars") or 0) > 0)
    with_forks = sum(1 for o in trending_rows if (o.get("forks") or 0) > 0)
    with_lang = sum(1 for o in trending_rows if o.get("primary_language"))
    with_gfi = sum(1 for o in opportunities if o.get("top_issues"))
    with_strategy = sum(1 for o in opportunities if o.get("reachout_strategy"))
    with_difficulty = sum(1 for o in opportunities if o.get("difficulty"))

    # Detect possible GitHub rate-limit fallback: when trending works but
    # GFI returns nothing, the upstream API has likely 403'd. The cache
    # could mask that on a warm process, so we surface the cache hit count
    # alongside the warning.
    cache_info = _github_issues._cached_search.cache_info()
    rate_limit_warn = len(gfi_rows) == 0 and bool(opportunities)

    print(f"\n=== OSS LIVE SMOKE - 3 languages (python, typescript, go) ===")
    print(f"Elapsed:       {elapsed:.2f}s")
    print(f"Total opps:    {total}")
    print(f"  trending:                       {len(trending_rows)}")
    print(f"  github_issues (GFI only):       {len(gfi_rows)}")
    print(f"  with stars >0:                  {with_stars}/{len(trending_rows)}")
    print(f"  with forks >0:                  {with_forks}/{len(trending_rows)}")
    print(f"  with primary_language:          {with_lang}/{len(trending_rows)}")
    print(f"  with top_issues (GFI merged):   {with_gfi}/{total}")
    print(f"  with reachout_strategy:         {with_strategy}/{total}")
    print(f"  with difficulty:                {with_difficulty}/{total}")

    # PASS/FAIL summary
    bs4_ok = with_stars >= max(1, len(trending_rows) // 2) and with_lang >= max(1, len(trending_rows) // 2)
    gfi_ok = with_gfi >= 1
    strategy_ok = with_strategy >= total - 1 and with_difficulty >= total - 1
    print(f"\nbs4 selectors match live HTML:  {'PASS' if bs4_ok else 'FAIL'}")
    print(f"GFI search returns real data:  {'PASS' if gfi_ok else 'FAIL'}")
    print(f"Strategy generator attached:   {'PASS' if strategy_ok else 'FAIL'}")
    print(f"GFI cache hits (this run):     {cache_info.hits}")
    if rate_limit_warn:
        print("WARN: trending rows present but no GFI rows — likely GitHub rate-limit (60/hr unauth)")

    # Sample top-3 trending rows
    print("\n--- Sample trending rows ---")
    for opp in trending_rows[:3]:
        print(json.dumps({
            "title": opp.get("title"),
            "stars": opp.get("stars"),
            "forks": opp.get("forks"),
            "primary_language": _safe(opp, "primary_language"),
            "difficulty": opp.get("difficulty"),
            "score": round(opp.get("score") or 0.0, 2),
            "top_issues_count": len(opp.get("top_issues") or []),
            "url": opp.get("url"),
        }, ensure_ascii=False))

    # Sample rows that merged top_issues (GFI success path)
    gfi_merged = [o for o in opportunities if (o.get("top_issues") or [])]
    print("\n--- Sample GFI-merged rows ---")
    for opp in gfi_merged[:2]:
        issues = opp.get("top_issues") or []
        sample_issues = [{"number": i.get("number"), "title": i.get("title")} for i in issues[:2]]
        print(json.dumps({
            "title": opp.get("title"),
            "score": round(opp.get("score") or 0.0, 2),
            "top_issues_count": len(issues),
            "top_issues_sample": sample_issues,
            "reachout_strategy_first120": (opp.get("reachout_strategy") or "")[:120],
        }, ensure_ascii=False))

    # Sample reachout template
    if opportunities:
        first = opportunities[0]
        print("\n--- Sample outreach template ---")
        print(f"Subject: {first.get('reachout_subject')}")
        body = (first.get('reachout_body') or "")[:280]
        print(f"Body: {body}...")


if __name__ == "__main__":
    main()
