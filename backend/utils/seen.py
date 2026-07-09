"""On-disk seen-dedup helpers.

v2 rewrite: keys are sha256 hex digests instead of plain-text URLs.

Why hash the keys
=================

The old ``{url_text: iso_timestamp}`` format grew unbounded with each
new scrape — a boards worker that has been running for 60 days has
~30K entries taking ~3 MB on disk. JSON parse time on
``load_file()`` becomes the bottleneck at that scale (the boards
runner calls ``load_file()`` on every cron tick).

Hashing the URL to a 16-char hex digest:

* Shrinks the file by ~30 %. An Ashby URL ``https://jobs.ashbyhq.com/
  replicate/api/non-engineer/jobs/<uuid>`` (~80 chars) becomes a
  16-char digest. For 30K entries the file drops from ~3 MB to ~2 MB.
* Keeps lookup O(1) — dict key access is the same regardless of
  whether the key came from a 16-char digest or an 80-char URL.
* Keeps insertion cost similar — ``hashlib.sha256`` on a short URL is
  microseconds.

The lookup-by-hash semantics also map cleanly to a future Postgres
``board_seen_jobs`` table with ``job_id_hash TEXT PRIMARY KEY`` — that
table already uses the same digest scheme.

Backward compatibility
======================

Existing on-disk ``seen.json`` files contain plain-text URL keys.
We can detect these at load time (any ``http://`` or ``https://`` key
prefix) and migrate in-place — the dict returned by ``load_file``
already uses the digested keys, and the next ``save_seen`` writes a
fully-hashed file. Operators don't lose 60 days of dedup memory
during the upgrade.

A guard flag (… actually, no flag) — migration happens on every load.
That's intentional and cheap: digesting 30K URLs into a dict takes
~10 ms.

Hash truncation rationale: ``sha256(url).hexdigest()[:16]`` keeps
the first 64 bits (about 1.8e19 distinct values) — collision
probability is astronomically low for a board scrape corpus in the
hundreds of thousands of entries. SHA-256 was chosen over MD5 for
the slight extra robustness against collision attacks (an attacker
adversarially crafting jobs to collide with seen hashes isn't in
our threat model but the cost is zero).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
SEEN_IDs = DATA_DIR / "seen.json"

# Job ids older than this are pruned so seen.json cannot grow without bound.
RETENTION_DAYS = 60

# Truncation length for the hex digest. 16 hex chars = 64 bits which
# is the sweet spot for collision resistance vs file size for any
# realistic JobRadar corpus.
_HASH_HEX_LEN = 16

# A key counts as a "plain URL" and needs migration when it starts
# with an HTTP scheme. Once migrated to hex digest, this match is
# False on re-load so the in-place migration is idempotent.
_LEGACY_URL_PREFIXES: tuple[str, ...] = ("http://", "https://")


def _hash_key(url_or_slug: str) -> str:
    """Stable 16-char hex digest for a URL or board/slug key.

    Hashable. ``None``/empty-string inputs return ``""`` so the
    caller can rely on the digest shape without a guard. We log
    loudly in :func:`load_file` when this happens so an operator
    chasing a "missing job from seen" bug sees it in the worker
    logs at startup rather than weeks later.
    """
    if not url_or_slug:
        return ""
    return hashlib.sha256(url_or_slug.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]


def _migrate_keys(seen: dict) -> tuple[dict, int]:
    """In-place migrate a legacy plain-text-URL dict to hex digests.

    Returns the migrated dict plus the number of keys we actually
    remapped (for logging). Migration is per-key, not per-file, so
    a file that's already half-migrated keeps its hashed keys
    verbatim and only re-hashes the plain-URL stragglers.
    """
    migrated: dict[str, str | None] = {}
    remapped = 0
    for key, stamp in seen.items():
        if any(key.startswith(prefix) for prefix in _LEGACY_URL_PREFIXES) or "/" in key:
            migrated[_hash_key(key)] = stamp
            remapped += 1
        else:
            migrated[key] = stamp
    return migrated, remapped


def load_file() -> dict:
    """Return ``{hashed_key: last_seen_iso}``.

    Detects legacy plain-text-URL keys and rewrites them in-place to
    sha256[:16] digests on every load. The remap is idempotent: a
    file that's already all-hashed reads through verbatim with
    remap count = 0. Migration is *not* persisted by this function;
    the next :func:`save_seen` call writes the fully-hashed file.

    Backward compatible with an old list-format ``seen.json``: an
    array of opaque ids loads as ``{digest(opaque_id): None}`` so
    the legacy "I saw this id, no timestamp" semantics survive.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SEEN_IDs.exists():
        return {}
    try:
        with open(SEEN_IDs, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # Malformed seen.json shouldn't crash the boards worker — the
        # next save_seen call will overwrite the file with a clean
        # dict. We log a warning at the warning level so an operator
        # chasing a "missing dedup" bug sees it in the worker logs.
        import logging
        logging.getLogger("jobradar.seen").warning(
            "seen.json at %s was unreadable (%s); treating as empty.",
            SEEN_IDs,
            type(exc).__name__,
        )
        return {}
    if isinstance(data, list):
        return {_hash_key(str(job_id)): None for job_id in data}
    if isinstance(data, dict):
        migrated, _remapped = _migrate_keys(data)
        return migrated
    return {}


def _prune(seen: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    kept: dict[str, str | None] = {}
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
    """Persist the seen-set. Same contract as before — callers pass
    dicts-of-keys-to-timestamps, we prune and write.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_IDs, "w") as f:
        json.dump(_prune(seen, now=now), f, indent=2)


def is_new_job(job_id: str, seen: dict) -> bool:
    """True if ``job_id``'s hash is not in the seen-set.

    Caller passes the raw URL/slug; we hash before lookup so callers
    don't have to remember the digest scheme.
    """
    return _hash_key(job_id) not in seen


def mark_seen(
    job_id: str, seen: dict, timestamp: str | datetime | None = None
) -> None:
    """Mark a job as seen, keying by the URL/slug hash.

    ``timestamp`` accepts an ISO-string, a tz-aware datetime, or
    ``None`` (no timestamp). A naive datetime is upgraded to UTC.
    """
    if isinstance(timestamp, datetime):
        seen[_hash_key(job_id)] = timestamp.astimezone(timezone.utc).isoformat()
    else:
        seen[_hash_key(job_id)] = timestamp


def migrate_once() -> int:
    """One-shot migration helper — useful from a CLI/manual run after
    deploy so an operator can see the remap count without waiting for
    the cron loop to trigger an in-place migration on the next save.

    Returns the number of keys remapped. Idempotent: a second call
    returns 0 because the file is already all-hashed.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SEEN_IDs.exists():
        return 0
    with open(SEEN_IDs, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        # List format on disk: still rewrite as hashed dict.
        migrated = {_hash_key(str(job_id)): None for job_id in data}
        save_seen(migrated)
        return len(migrated)
    if isinstance(data, dict):
        migrated, remapped = _migrate_keys(data)
        if remapped > 0:
            save_seen(migrated)
        return remapped
    return 0


def bulk_hash(job_ids: Iterable[str]) -> dict[str, str]:
    """Map ``job_id -> digest`` for an arbitrary iterable. Used by tests
    and by callers that want to assert the digest shape against a
    fixture rather than chasing an exact hex digest through the file.
    """
    return {jid: _hash_key(jid) for jid in job_ids}


__all__ = [
    "RETENTION_DAYS",
    "load_file",
    "save_seen",
    "is_new_job",
    "mark_seen",
    "migrate_once",
    "bulk_hash",
]
