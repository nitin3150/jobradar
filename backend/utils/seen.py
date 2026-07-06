import json
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
SEEN_IDs = DATA_DIR / "seen.json"


def load_file():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SEEN_IDs.exists():
        return set()
    with open(SEEN_IDs, "r") as f:
        return set(json.load(f))


def save_seen(seen_set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_IDs, "w") as f:
        json.dump(list(seen_set), f, indent=2)


def is_new_job(job_id: str, seen_set: set) -> bool:
    return job_id not in seen_set


def mark_seen(job_id: str, seen_set: set):
    seen_set.add(job_id)