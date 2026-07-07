import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
SEEN_IDs = DATA_DIR / "seen.json"

# Job ids older than this are pruned so seen.json cannot grow without bound.
RETENTION_DAYS = 60


def load_file() -> dict:
    """Return {job_id: last_seen_iso}.

    Backward compatible: an old list-format seen.json loads as ids with unknown
    age (value ``None``), which are kept until re-seen with a timestamp.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SEEN_IDs.exists():
        return {}
    with open(SEEN_IDs, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {str(job_id): None for job_id in data}
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items()}
    return {}


def _prune(seen: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    kept = {}
    for job_id, stamp in seen.items():
        if stamp is None:
            kept[job_id] = None  # unknown age -> keep (pre-existing ids)
            continue
        try:
            when = datetime.fromisoformat(stamp)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            kept[job_id] = stamp
            continue
        if when >= cutoff:
            kept[job_id] = stamp
    return kept


def save_seen(seen: dict, now: datetime | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_IDs, "w") as f:
        json.dump(_prune(seen, now=now), f, indent=2)


def is_new_job(job_id: str, seen: dict) -> bool:
    return job_id not in seen


def mark_seen(job_id: str, seen: dict, timestamp=None) -> None:
    if isinstance(timestamp, datetime):
        seen[job_id] = timestamp.astimezone(timezone.utc).isoformat()
    else:
        seen[job_id] = timestamp
