from utils.http import get_json
from utils.time_check import parse_published_at


def fetch(slug: str, *, client, since=None, seen_ids=frozenset()):
    data = get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    latest_timestamp = None
    discovered_jobs = []
    new_ids: dict[str, str] = {}

    for job in data.get("jobs", []):
        timestamp_value = job.get("updated_at") or job.get("published_at") or job.get("date_posted")
        if not timestamp_value:
            continue
        timestamp = parse_published_at(timestamp_value)
        if since is not None and timestamp < since:
            continue
        if latest_timestamp is None or timestamp > latest_timestamp:
            latest_timestamp = timestamp

        job_id = job.get("id")
        if job_id is None:
            continue
        job_id = str(job_id)
        if job_id in seen_ids or job_id in new_ids:
            continue
        new_ids[job_id] = timestamp.isoformat()
        discovered_jobs.append({
            "id": job_id,
            "title": job.get("title"),
            "url": job.get("absolute_url"),
            "published_at": timestamp,
            "description": job.get("content") or "",
        })

    return {
        "jobs": discovered_jobs,
        "new_ids": new_ids,
        "latest": latest_timestamp.isoformat() if latest_timestamp else None,
    }
