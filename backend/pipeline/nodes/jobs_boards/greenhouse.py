from utils.http import get_json
from utils.time_check import parse_published_at


def fetch(slug: str, *, client, since=None, seen_ids=frozenset()):
    """Fetch new jobs for a Greenhouse org.

    Pure function: takes a snapshot of ``seen_ids``, returns
    ``{jobs, new_ids, latest}`` describing what the org posted
    since the cutoff. HTTP errors propagate so the runner can
    classify them (404/410 -> ``outcome="missing"``).

    Why the two-timestamp split
    ===========================

    A Greenhouse record keeps TWO separate timestamps:

    * ``updated_at`` — bumped on every write to the row (recruiter
      edits, reposts, content tweaks, even an automated
      re-validation that doesn't change the visible description).
    * ``published_at`` (sometimes also ``date_posted``) — the
      IMMUTABLE first-posted date of the listing.

    The previous one-timestamp loop conflated the two: it took
    ``updated_at or published_at or date_posted`` and used that same
    value for BOTH the since-gate AND the ``published_at`` field on
    the returned job dict. The bug that produced the operator's
    "old jobs with new dates" report: when Greenhouse silently
    touched an old posting (e.g. a content re-validation), the
    fresh ``updated_at`` passed the since-gate AND got returned as
    the job's ``posted_at`` — so a posting originally from weeks
    ago landed in the review queue dated today.

    Fix: the since-gate still prefers ``updated_at`` (it's the
    most-recent-activity signal we have, and is what filters "has
    the org done anything I haven't seen?"), but the
    ``published_at`` value on the returned payload prefers
    ``published_at`` (then ``date_posted``) and ONLY falls back to
    ``updated_at`` when nothing else is set. The two-timestamp
    split preserves Greenhouse's "the job was originally posted
    on date X" semantics while still respecting the "did I miss
    an update?" gate.
    """
    data = get_json(
        client,
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    )

    latest_timestamp = None
    discovered_jobs = []
    new_ids: dict[str, str] = {}

    for job in data.get("jobs", []):
        # Gate timestamp — most-recent-activity. Updated_at wins
        # because it's the highest-fidelity freshness signal
        # Greenhouse exposes; if a row is re-touched we'll see it
        # in the next 1h/24h window.
        gate_value = (
            job.get("updated_at")
            or job.get("published_at")
            or job.get("date_posted")
        )
        if not gate_value:
            continue
        gate_timestamp = parse_published_at(gate_value)
        if since is not None and gate_timestamp < since:
            continue

        # Published-at value — surfaces the ORIGINAL posting date.
        # Order matters: published_at wins, date_posted wins next
        # (Greenhouse gives both on most listings), and updated_at
        # is the LAST-resort fallback ONLY when both are absent.
        # This is the field the runner carries forward to the
        # ``posted_at`` DB column and that the React JobCard
        # renders as the "Posted on" subtitle.
        posted_value = (
            job.get("published_at")
            or job.get("date_posted")
            or job.get("updated_at")
        )
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
            "title": job.get("title"),
            "url": job.get("absolute_url"),
            # ``published_at`` is the immutable first-posting date
            # (per ATS semantics above); when neither published_at
            # nor date_posted is exposed we fall back to
            # updated_at because something is better than nothing.
            "published_at": published_at,
            "description": job.get("content") or "",
        })

    return {
        "jobs": discovered_jobs,
        "new_ids": new_ids,
        "latest": latest_timestamp.isoformat() if latest_timestamp else None,
    }
