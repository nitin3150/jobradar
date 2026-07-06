import httpx

from utils.seen import is_new_job, mark_seen, load_file, save_seen
from utils.time_check import parse_published_at, time_check


def fetch(slug: str, since=None, seen_jobs=None, org_last_posted=None):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    res = httpx.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    seen = seen_jobs or load_file()
    latest_timestamp = None
    discovered_jobs = []

    for job in data:
        timestamp_value = job.get("createdAt") or job.get("created_at") or job.get("updatedAt")
        if not timestamp_value:
            continue
        timestamp = parse_published_at(timestamp_value)
        if since is not None and timestamp < since:
            continue

        if latest_timestamp is None or timestamp > latest_timestamp:
            latest_timestamp = timestamp

        job_id = str(job["id"])
        if is_new_job(job_id, seen):
            mark_seen(job_id, seen)
            discovered_jobs.append({
                "id": job_id,
                "title": job.get("text"),
                "url": job.get("hostedUrl"),
                "published_at": timestamp,
            })

    if org_last_posted is not None and latest_timestamp is not None:
        org_last_posted[slug] = latest_timestamp.isoformat()

    if seen_jobs is None:
        save_seen(seen)

    return discovered_jobs