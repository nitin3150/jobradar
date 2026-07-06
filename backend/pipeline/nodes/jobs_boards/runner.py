import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pipeline.nodes.jobs_boards.ashby import fetch as ashby_fetch
from pipeline.nodes.jobs_boards.greenhouse import fetch as greenhouse_fetch
from pipeline.nodes.jobs_boards.lever import fetch as lever_fetch
from utils.filters import filter_roles
from utils.seen import load_file, save_seen
from utils.time_check import parse_published_at

DATA_DIR = BACKEND_ROOT / "data"
ORG_INDEX = {
    "ashby": (DATA_DIR / "ashby_companies.json", ashby_fetch),
    "greenhouse": (DATA_DIR / "greenhouse_companies.json", greenhouse_fetch),
    "lever": (DATA_DIR / "lever_companies.json", lever_fetch),
}


def compute_since_cutoff(now=None, delta_hours=1, last_run=None):
    now = now or datetime.now(timezone.utc)
    if last_run is not None:
        return max(last_run, now - timedelta(hours=delta_hours))
    return now - timedelta(hours=delta_hours)


def load_orgs(board_name):
    path = ORG_INDEX[board_name][0]
    with open(path, "r") as handle:
        orgs = json.load(handle)

    missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
    if missing_path.exists():
        with open(missing_path, "r") as handle:
            missing = set(json.load(handle))
        return [slug for slug in orgs if slug not in missing]
    return orgs


def load_last_run_state(path=None):
    path = path or DATA_DIR / "last_run.json"
    if not path.exists():
        return {}
    with open(path, "r") as handle:
        return json.load(handle)


def save_last_run_state(state, path=None):
    path = path or DATA_DIR / "last_run.json"
    with open(path, "w") as handle:
        json.dump(state, handle, indent=2)


def update_missing_orgs(board_name, slug, missing_orgs):
    missing_orgs.setdefault(board_name, []).append(slug)


def execute_fetch(fetcher, board_name, slug, since, seen_jobs, org_last_posted, missing_orgs):
    try:
        return fetcher(slug, since=since, seen_jobs=seen_jobs, org_last_posted=org_last_posted)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {404, 410}:
            update_missing_orgs(board_name, slug, missing_orgs)
            return []
        print(f"HTTP error while scraping {board_name}/{slug}: {exc}")
        return []
    except httpx.TimeoutException:
        print(f"Request timed out while scraping {board_name}/{slug}")
        return []
    except Exception as exc:
        print(f"Scraper error for {board_name}/{slug}: {exc}")
        return []


def run_all(delta_hours=1, boards=None, limit=None):
    boards = boards or list(ORG_INDEX.keys())
    seen_jobs = load_file()
    last_run_state = load_last_run_state()
    last_run_timestamp = None
    if last_run_state.get("last_run"):
        last_run_timestamp = parse_published_at(last_run_state["last_run"])

    since = compute_since_cutoff(delta_hours=delta_hours, last_run=last_run_timestamp)

    results = []
    org_last_posted = {}
    missing_orgs = {}

    with ThreadPoolExecutor(max_workers=min(8, len(boards) * 3)) as executor:
        futures = []
        for board_name in boards:
            orgs = load_orgs(board_name)
            if limit is not None:
                orgs = orgs[:limit]
            fetcher = ORG_INDEX[board_name][1]
            for slug in orgs:
                futures.append(
                    executor.submit(
                        execute_fetch,
                        fetcher,
                        board_name,
                        slug,
                        since,
                        seen_jobs,
                        org_last_posted,
                        missing_orgs,
                    )
                )

        for future in as_completed(futures):
            results.extend(future.result())

    for board_name, slugs in missing_orgs.items():
        missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
        previous = []
        if missing_path.exists():
            with open(missing_path, "r") as handle:
                previous = json.load(handle)
        combined = sorted(set(previous) | set(slugs))
        with open(missing_path, "w") as handle:
            json.dump(combined, handle, indent=2)

    filtered_results = filter_roles(results)

    save_seen(seen_jobs)
    save_last_run_state({
        "last_run": datetime.now(timezone.utc).isoformat(),
        "org_last_posted": org_last_posted,
    })
    return filtered_results


def main():
    parser = argparse.ArgumentParser(description="Run all configured job-board scrapers")
    parser.add_argument("--delta-hours", type=int, default=1)
    parser.add_argument("--boards", nargs="*", default=list(ORG_INDEX.keys()))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_all(delta_hours=args.delta_hours, boards=args.boards, limit=args.limit)


if __name__ == "__main__":
    main()