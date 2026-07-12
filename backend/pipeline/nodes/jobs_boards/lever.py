from utils.http import get_json
from utils.time_check import parse_published_at


def fetch(slug: str, *, client, since=None, seen_ids=frozenset()):
    """Fetch new jobs for a Lever org.

    Lever distinguishes ``createdAt`` (immutable posting date) from
    ``updatedAt`` (bumped on every edit). Bug-fix rationale mirrors
    :func:`greenhouse.fetch`: the since-gate may use the more recent
    of the two so a content-revalidation triggers re-evaluation,
    but the ``published_at`` value on the returned payload prefers
    the immutable ``createdAt`` so a silent edit doesn't bump the
    posting date.
    """
    data = get_json(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    latest_timestamp = None
    discovered_jobs = []
    new_ids: dict[str, str] = {}

    for job in data:
        # Gate: most-recent of createdAt/updatedAt (whichever fires
        # the since filter). updatedAt second because it represents
        # a content-touch event, which the operator wants to see.
        gate_value = job.get("updatedAt") or job.get("createdAt") or job.get("created_at")
        if not gate_value:
            continue
        gate_timestamp = parse_published_at(gate_value)
        if since is not None and gate_timestamp < since:
            continue

        # Published-at value: createdAt/created_at (the immutable
        # first-posted date). updatedAt is NOT preferred here — if
        # Lever edits a row silently we don't want to bump the
        # "Posted on" date visible in the React JobCard.
        posted_value = job.get("createdAt") or job.get("created_at")
        published_at = parse_published_at(posted_value) if posted_value else None

        if latest_timestamp is None or gate_timestamp > latest_timestamp:
            latest_timestamp = gate_timestamp

        job_id = job.get("id")
        if job_id is None:
            continue
        job_id = str(job_id)
        if job_id in seen_ids or job_id in new_ids:
            continue
        new_ids[job_id] = gate_timestamp.isoformat()
        discovered_jobs.append({
            "id": job_id,
            "title": job.get("text"),
            "url": job.get("hostedUrl"),
            "published_at": published_at,
            "description": job.get("descriptionPlain") or job.get("description") or "",
        })

    return {
        "jobs": discovered_jobs,
        "new_ids": new_ids,
        "latest": latest_timestamp.isoformat() if latest_timestamp else None,
    }
