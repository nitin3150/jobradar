from utils.http import get_json
from utils.time_check import parse_published_at


def fetch(slug: str, *, client, since=None, seen_ids=frozenset()):
    """Fetch new jobs for an ashby org.

    Pure: reads a read-only ``seen_ids`` snapshot, mutates nothing shared, and
    returns discovered jobs plus the ids to record. HTTP errors propagate so the
    runner can classify them (404/410 -> missing).
    """
    data = get_json(
        client,
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    )
    latest_timestamp = None
    discovered_jobs = []
    new_ids: dict[str, str] = {}

    for job in data.get("jobs", []):
        published = job.get("publishedAt")
        if not published:
            continue
        timestamp = parse_published_at(published)
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
            "url": job.get("jobUrl"),
            "published_at": timestamp,
            "description": job.get("descriptionPlain") or job.get("description") or "",
        })

    return {
        "jobs": discovered_jobs,
        "new_ids": new_ids,
        "latest": latest_timestamp.isoformat() if latest_timestamp else None,
    }
