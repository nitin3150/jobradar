import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx

DATA_DIR = BACKEND_ROOT / "data"


def check_missing_orgs(board_name, orgs):
    missing = []
    for slug in orgs:
        url = {
            "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
            "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        }[board_name]
        try:
            response = httpx.get(url, timeout=10)
            response.raise_for_status()
        except Exception:
            missing.append(slug)
    return missing


def main():
    for board_name in ("ashby", "greenhouse", "lever"):
        path = DATA_DIR / f"{board_name}_companies.json"
        with open(path, "r") as handle:
            orgs = json.load(handle)
        missing = check_missing_orgs(board_name, orgs)
        active_orgs = [slug for slug in orgs if slug not in set(missing)]
        with open(path, "w") as handle:
            json.dump(active_orgs, handle, indent=2)

        missing_path = DATA_DIR / f"{board_name}_missing_orgs.json"
        with open(missing_path, "w") as handle:
            json.dump(missing, handle, indent=2)

        print(f"Missing orgs for {board_name}: {len(missing)}")
        if missing:
            print(missing[:20])


if __name__ == "__main__":
    main()
