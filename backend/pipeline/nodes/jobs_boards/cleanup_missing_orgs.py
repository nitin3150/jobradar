"""Reassign missing org slugs to the board that can actually serve their jobs.

Every board keeps two files in ``data/``:

* ``<board>_companies.json`` -- slugs the scanner scrapes.
* ``<board>_missing_orgs.json`` -- slugs whose own board API 404'd.

A slug parked in one board's missing list is frequently reachable on a
*different* board (e.g. a company listed under greenhouse that actually hosts
its board on ashby). This tool re-probes every missing slug against all three
board APIs, and:

* moves reachable slugs onto the board that serves them (priority
  ashby > greenhouse > lever), fixing the companies + missing lists;
* records slugs reachable on no board (self-hosted careers only) to
  ``own_careers_only.json`` for later handling.

Run ``--dry-run`` (default) to preview, ``--apply`` to write the files.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx

DATA_DIR = BACKEND_ROOT / "data"
PROBE_CACHE_PATH = DATA_DIR / "verified_org_targets.json"
CAREERS_ONLY_PATH = DATA_DIR / "own_careers_only.json"

# ashby wins ties (richest API, includes compensation), then greenhouse, lever.
BOARD_PRIORITY = ["ashby", "greenhouse", "lever"]

MAX_CONCURRENCY = 30
REQUEST_TIMEOUT = 5.0


def board_api_url(board: str, slug: str) -> str:
    if board == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    if board == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if board == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    raise ValueError(f"unknown board: {board}")


def response_matches_board(board: str, response: httpx.Response) -> bool:
    """True only if the response is a live board of ``board``'s shape.

    A bare ``status < 400`` is not enough: slug collisions and generic error
    pages can look reachable. We require the payload to parse as the board's
    documented shape. A live board with zero postings (``{"jobs": []}``) still
    counts -- the board exists, it just has nothing open right now.
    """
    if response.status_code >= 400:
        return False
    try:
        data = response.json()
    except Exception:
        return False
    if board in ("ashby", "greenhouse"):
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)
    if board == "lever":
        return isinstance(data, list)
    return False


def choose_target(reachable: list[str]) -> str | None:
    for board in BOARD_PRIORITY:
        if board in reachable:
            return board
    return None


async def probe_org(slug: str, client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> list[str]:
    """Return the boards (subset of BOARD_PRIORITY) whose API serves ``slug``."""

    async def check(board: str) -> str | None:
        async with semaphore:
            try:
                response = await client.get(board_api_url(board, slug))
            except Exception:
                return None
            return board if response_matches_board(board, response) else None

    results = await asyncio.gather(*[check(board) for board in BOARD_PRIORITY])
    return [board for board in results if board]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    with open(path, "w") as handle:
        json.dump(value, handle, indent=2)


def gather_missing_slugs() -> tuple[dict[str, list[str]], list[str]]:
    missing = {
        board: load_json(DATA_DIR / f"{board}_missing_orgs.json", [])
        for board in BOARD_PRIORITY
    }
    union: list[str] = sorted({slug for slugs in missing.values() for slug in slugs})
    return missing, union


async def probe_all(slugs: list[str], resume: bool = True) -> dict[str, list[str]]:
    """Probe every slug against all boards, checkpointing to the cache file."""
    cache: dict[str, list[str]] = {}
    if resume:
        for entry in load_json(PROBE_CACHE_PATH, []):
            if isinstance(entry, dict) and "org" in entry and "reachable" in entry:
                cache[entry["org"]] = entry["reachable"]

    todo = [slug for slug in slugs if slug not in cache]
    print(f"probing {len(todo)} slugs ({len(cache)} cached, {len(slugs)} total)")

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        for start in range(0, len(todo), 100):
            chunk = todo[start : start + 100]
            reachable_lists = await asyncio.gather(
                *[probe_org(slug, client, semaphore) for slug in chunk]
            )
            for slug, reachable in zip(chunk, reachable_lists):
                cache[slug] = reachable
            save_json(
                PROBE_CACHE_PATH,
                [{"org": slug, "reachable": reachable} for slug, reachable in sorted(cache.items())],
            )
            print(f"probed {min(start + len(chunk), len(todo))}/{len(todo)}")

    return {slug: cache[slug] for slug in slugs}


def plan_moves(probe_results: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (moves, careers_only) from probe results.

    A move records where a slug should live. careers_only are slugs reachable on
    no board -- deferred to a report, not scraped here.
    """
    moves: list[dict[str, Any]] = []
    careers_only: list[str] = []
    for slug, reachable in probe_results.items():
        target = choose_target(reachable)
        if target is None:
            careers_only.append(slug)
        else:
            moves.append({"org": slug, "target": target, "reachable": reachable})
    return moves, sorted(careers_only)


def apply_moves(moves: list[dict[str, Any]], careers_only: list[str], dry_run: bool) -> None:
    companies = {
        board: set(load_json(DATA_DIR / f"{board}_companies.json", []))
        for board in BOARD_PRIORITY
    }
    missing = {
        board: set(load_json(DATA_DIR / f"{board}_missing_orgs.json", []))
        for board in BOARD_PRIORITY
    }

    for move in moves:
        slug, target = move["org"], move["target"]
        companies[target].add(slug)
        missing[target].discard(slug)
        for board in BOARD_PRIORITY:
            if board == target:
                continue
            companies[board].discard(slug)
            missing[board].discard(slug)

    if dry_run:
        print("\n-- DRY RUN (no files written; pass --apply to write) --")
    else:
        for board in BOARD_PRIORITY:
            save_json(DATA_DIR / f"{board}_companies.json", sorted(companies[board]))
            save_json(DATA_DIR / f"{board}_missing_orgs.json", sorted(missing[board]))
        save_json(CAREERS_ONLY_PATH, careers_only)
        print(f"\nwrote companies + missing lists; {CAREERS_ONLY_PATH.name} = {len(careers_only)} slugs")


def summarize(moves: list[dict[str, Any]], careers_only: list[str]) -> None:
    per_target: dict[str, int] = {board: 0 for board in BOARD_PRIORITY}
    multi = 0
    for move in moves:
        per_target[move["target"]] += 1
        if len(move["reachable"]) > 1:
            multi += 1
    print("\n=== summary ===")
    print(f"reassigned : {len(moves)}")
    for board in BOARD_PRIORITY:
        print(f"  -> {board}: {per_target[board]}")
    print(f"reachable on >1 board (priority applied): {multi}")
    print(f"careers-only (no board): {len(careers_only)}")
    for move in moves[:15]:
        others = [b for b in move["reachable"] if b != move["target"]]
        note = f" (also {', '.join(others)})" if others else ""
        print(f"  {move['org']} -> {move['target']}{note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write files (default: dry run)")
    parser.add_argument("--no-resume", action="store_true", help="ignore probe cache, re-probe all")
    args = parser.parse_args()

    _, union = gather_missing_slugs()
    if not union:
        print("no missing slugs to process")
        return

    probe_results = asyncio.run(probe_all(union, resume=not args.no_resume))
    moves, careers_only = plan_moves(probe_results)
    summarize(moves, careers_only)
    apply_moves(moves, careers_only, dry_run=not args.apply)


if __name__ == "__main__":
    main()
