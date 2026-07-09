"""Tests for :mod:`utils.seen` — the on-disk seen-dedup helper.

v2 rewrite moved from plain-text URL keys to ``sha256 hex[:16]``
digests so a 30K-entry ``seen.json`` shrinks ~30 % and parse time
drops correspondingly. This module exercises:

* :func:`_hash_key` — deterministic, 16-char hex, unique per input.
* :func:`_migrate_keys` — re-hashes legacy plain-URL keys; passes
  through already-hashed hex digests unchanged.
* :func:`load_file` — returns ``{hashed_key: stamp}`` regardless of
  whether the on-disk file is the legacy or v2 shape.
* :func:`save_seen` — writes the dict back; legacy keys re-hashed
  in the process.
* :func:`migrate_once` — one-shot CLI helper for operators who
  want a deterministic remap count; idempotent.
* :func:`is_new_job` / :func:`mark_seen` — round-trip semantics
  work with the same input the boards runner would have passed
  before the rewrite.

The tests use a ``tmp_path`` fixture-style :func:`_with_temp_dir`
context manager that monkey-patches the module-level ``DATA_DIR`` and
``SEEN_IDs`` paths so the production ``backend/data/seen.json`` is
untouched. Without that swap, a test that asserts the file is empty
would wipe the real corpus and the next boards-scan would re-deliver
60 days of dedup memory.
"""
from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from pathlib import Path

import utils.seen as seen


@contextmanager
def _with_temp_dir(tmp_path: Path):
    """Swap :mod:`utils.seen` to use ``tmp_path`` for the duration of
    a test. Restores the production paths on exit so a test that
    aborts mid-run doesn't leave a monkey-patch in place for the
    next suite.
    """
    saved_dir = seen.DATA_DIR
    saved_path = seen.SEEN_IDs
    seen.DATA_DIR = tmp_path
    seen.SEEN_IDs = tmp_path / "seen.json"
    try:
        yield tmp_path
    finally:
        seen.DATA_DIR = saved_dir
        seen.SEEN_IDs = saved_path


# ---------------------------------------------------------------------------
class TestHashKey(unittest.TestCase):
    def test_hash_is_deterministic(self) -> None:
        url = "https://jobs.ashbyhq.com/replicate/abc123"
        self.assertEqual(seen._hash_key(url), seen._hash_key(url))

    def test_hash_is_16_chars(self) -> None:
        h = seen._hash_key("https://example.com/jobs/1")
        self.assertEqual(len(h), 16)

    def test_hash_is_hex(self) -> None:
        h = seen._hash_key("https://example.com/jobs/1")
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_different_inputs_produce_different_hashes(self) -> None:
        a = seen._hash_key("https://example.com/a")
        b = seen._hash_key("https://example.com/b")
        self.assertNotEqual(a, b)

    def test_empty_string_returns_empty(self) -> None:
        self.assertEqual(seen._hash_key(""), "")

    def test_hash_matches_sha256_prefix(self) -> None:
        # The first 16 chars of sha256("abc") hex digest are
        # ``ba7816bf8f01cfea414140de5dae2223b00361a3`` — pinning
        # the algorithm in a regression test so a future refactor
        # that swaps SHA-256 for SHA-1 or a different digest length
        # fails loudly here.
        import hashlib
        expected = hashlib.sha256(b"abc").hexdigest()[:16]
        self.assertEqual(seen._hash_key("abc"), expected)


# ---------------------------------------------------------------------------
class TestMigrateKeys(unittest.TestCase):
    def test_url_key_is_hashed(self) -> None:
        legacy = {"https://example.com/jobs/1": "2026-01-01T00:00:00Z"}
        migrated, count = seen._migrate_keys(legacy)
        self.assertEqual(count, 1)
        self.assertNotIn("https://example.com/jobs/1", migrated)
        self.assertIn(seen._hash_key("https://example.com/jobs/1"), migrated)
        self.assertEqual(migrated[seen._hash_key("https://example.com/jobs/1")], "2026-01-01T00:00:00Z")

    def test_url_with_path_segments_is_hashed(self) -> None:
        # The migration check fires on either ``http(s)://`` prefix
        # OR the presence of ``/`` — so URLs with path segments are
        # caught even when the on-disk file dropped the scheme.
        legacy = {"jobs.example.com/role/12345": "2026-01-01T00:00:00Z"}
        migrated, count = seen._migrate_keys(legacy)
        self.assertEqual(count, 1)
        self.assertIn(seen._hash_key("jobs.example.com/role/12345"), migrated)

    def test_hex_digest_passes_through(self) -> None:
        # Idempotency guard: a re-load of an already-hashed file
        # must not re-hash the keys.
        existing_hash = seen._hash_key("https://example.com/jobs/1")
        already_hashed = {existing_hash: "2026-01-01T00:00:00Z"}
        migrated, count = seen._migrate_keys(already_hashed)
        self.assertEqual(count, 0)
        self.assertEqual(migrated, already_hashed)

    def test_colon_separated_board_slug_passes_through(self) -> None:
        # The legacy ``ashby:replicate`` (board:slug) format
        # doesn't start with http and has no ``/`` so the
        # migration skips it. This is a documented cosmetic
        # limitation — the colon-separated key persists until a
        # future refactor retags the on-disk format.
        legacy = {"ashby:replicate": "2026-01-01T00:00:00Z"}
        migrated, count = seen._migrate_keys(legacy)
        self.assertEqual(count, 0)
        self.assertEqual(migrated, legacy)

    def test_none_timestamp_passes_through(self) -> None:
        # ``None`` timestamps (legacy ids whose age is unknown) get
        # carried verbatim into the migrated dict.
        legacy = {"https://example.com/jobs/1": None}
        migrated, count = seen._migrate_keys(legacy)
        self.assertEqual(count, 1)
        self.assertIsNone(migrated[seen._hash_key("https://example.com/jobs/1")])

    def test_mixed_legacy_and_hashed_keys(self) -> None:
        # Real-world file mid-migration: some keys URL, some already
        # hex. The migration only re-hashes the legacy ones; the
        # remap count is the number of legacy keys.
        existing_hash = seen._hash_key("https://example.com/jobs/1")
        mixed = {
            "https://example.com/jobs/2": "2026-01-01T00:00:00Z",
            existing_hash: "2026-01-01T00:00:00Z",
        }
        migrated, count = seen._migrate_keys(mixed)
        self.assertEqual(count, 1)
        self.assertEqual(set(migrated.keys()), {
            seen._hash_key("https://example.com/jobs/2"),
            existing_hash,
        })


# ---------------------------------------------------------------------------
class TestLoadFile(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        with _with_temp_dir(Path("/tmp/seen-pytest-missing")) as tmp:
            self.assertEqual(seen.load_file(), {})

    def test_legacy_url_keys_get_hashed_on_load(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            (tmp / "seen.json").write_text(json.dumps({
                "https://jobs.ashbyhq.com/replicate/1": "2026-01-01T00:00:00Z",
                "https://jobs.greenhouse.io/vercel/2": "2026-01-02T00:00:00Z",
            }))
            result = seen.load_file()
            self.assertEqual(len(result), 2)
            for original_url in (
                "https://jobs.ashbyhq.com/replicate/1",
                "https://jobs.greenhouse.io/vercel/2",
            ):
                self.assertIn(seen._hash_key(original_url), result)
                self.assertNotIn(original_url, result)

    def test_already_hashed_keys_load_verbatim(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            existing = {
                seen._hash_key("https://example.com/jobs/1"): "2026-01-01T00:00:00Z",
            }
            (tmp / "seen.json").write_text(json.dumps(existing))
            result = seen.load_file()
            self.assertEqual(result, existing)

    def test_legacy_list_format_loads_as_hashed_unknown_age(self) -> None:
        # Pre-dict on-disk format was a JSON array of opaque ids.
        # ``load_file`` back-compat: convert each to ``{digest: None}``
        # so the unknown-age rows are kept until a fresh ``mark_seen``
        # writes a real timestamp.
        with _with_temp_dir(_fresh_tmp()) as tmp:
            (tmp / "seen.json").write_text(json.dumps([
                "ashby:replicate/abc123",
                "ashby:replicate/def456",
            ]))
            result = seen.load_file()
            self.assertEqual(len(result), 2)
            for legacy in ("ashby:replicate/abc123", "ashby:replicate/def456"):
                self.assertIn(seen._hash_key(legacy), result)
                self.assertIsNone(result[seen._hash_key(legacy)])

    def test_invalid_on_disk_shape_returns_empty(self) -> None:
        # A malformed file (string, number, etc.) shouldn't crash the
        # worker — we just return an empty seen-set and the next
        # boards-scan will repopulate it.
        with _with_temp_dir(_fresh_tmp()) as tmp:
            (tmp / "seen.json").write_text("not-json")
            self.assertEqual(seen.load_file(), {})


# ---------------------------------------------------------------------------
class TestSaveSeen(unittest.TestCase):
    def test_save_writes_hashed_keys(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            # Recent timestamp — the seen.py ``_prune`` drops entries
            # older than RETENTION_DAYS (60) so a 2026-01-01 stamp
            # gets filtered out at boot time (2026-07+). Use a stamp
            # within the retention window so the assertion actually
            # sees the on-disk row.
            from datetime import datetime, timezone
            recent = datetime.now(timezone.utc).isoformat()
            seen.save_seen({
                seen._hash_key("https://example.com/jobs/1"): recent,
            })
            on_disk = json.loads((tmp / "seen.json").read_text())
            self.assertIn(seen._hash_key("https://example.com/jobs/1"), on_disk)
            self.assertNotIn("https://example.com/jobs/1", on_disk)

    def test_save_creates_data_dir_if_missing(self) -> None:
        with _with_temp_dir(_fresh_tmp(autocreate=False)) as tmp:
            self.assertFalse((tmp / "data").exists())
            # Monkey-patch DATA_DIR to point at a *non-existent*
            # subdirectory of tmp so ``save_seen`` creates it.
            # The outer _with_temp_dir context manager restores the
            # production paths on exit, so no inner try/finally is
            # needed here.
            seen.DATA_DIR = tmp / "data"
            seen.SEEN_IDs = tmp / "data" / "seen.json"
            seen.save_seen({seen._hash_key("a"): "2026-01-01T00:00:00Z"})
            self.assertTrue((tmp / "data" / "seen.json").exists())


# ---------------------------------------------------------------------------
class TestMigrateOnce(unittest.TestCase):
    def test_migrate_once_hashes_url_keys_and_persists(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            # Recent timestamps (within the 60-day retention window)
            # so the post-migration save_seen doesn't prune them
            # away and the on-disk assertion actually sees the
            # migrated rows.
            from datetime import datetime, timezone
            recent1 = datetime.now(timezone.utc).isoformat()
            recent2 = datetime.now(timezone.utc).isoformat()
            (tmp / "seen.json").write_text(json.dumps({
                "https://example.com/jobs/1": recent1,
                "https://example.com/jobs/2": recent2,
            }))
            count = seen.migrate_once()
            self.assertEqual(count, 2)
            on_disk = json.loads((tmp / "seen.json").read_text())
            self.assertIn(seen._hash_key("https://example.com/jobs/1"), on_disk)
            self.assertNotIn("https://example.com/jobs/1", on_disk)

    def test_migrate_once_is_idempotent(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            (tmp / "seen.json").write_text(json.dumps({
                "https://example.com/jobs/1": "2026-01-01T00:00:00Z",
            }))
            first = seen.migrate_once()
            self.assertEqual(first, 1)
            # Second call: file is already fully-hashed, nothing to do.
            second = seen.migrate_once()
            self.assertEqual(second, 0)

    def test_migrate_once_with_missing_file_returns_zero(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            # ``_fresh_tmp`` does NOT create seen.json — ``migrate_once``
            # should treat a missing file as "nothing to migrate" and
            # return 0.
            self.assertEqual(seen.migrate_once(), 0)

    def test_migrate_once_legacy_list_format_rewrites_as_hashed(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            (tmp / "seen.json").write_text(json.dumps([
                "ashby:replicate/1",
                "ashby:replicate/2",
            ]))
            count = seen.migrate_once()
            self.assertEqual(count, 2)
            on_disk = json.loads((tmp / "seen.json").read_text())
            # The list is now a dict of hashed keys.
            self.assertIsInstance(on_disk, dict)
            self.assertEqual(len(on_disk), 2)
            self.assertIn(seen._hash_key("ashby:replicate/1"), on_disk)
            self.assertIsNone(on_disk[seen._hash_key("ashby:replicate/1")])


# ---------------------------------------------------------------------------
class TestMarkSeenIsNewJob(unittest.TestCase):
    def test_is_new_job_for_unseen_url(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            seen_dict = {}
            url = "https://example.com/jobs/abc"
            self.assertTrue(seen.is_new_job(url, seen_dict))
            seen.mark_seen(url, seen_dict)
            self.assertFalse(seen.is_new_job(url, seen_dict))

    def test_mark_seen_uses_hash_as_key(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            seen_dict = {}
            url = "https://example.com/jobs/abc"
            seen.mark_seen(url, seen_dict, "2026-01-01T00:00:00Z")
            self.assertIn(seen._hash_key(url), seen_dict)
            self.assertNotIn(url, seen_dict)

    def test_mark_seen_with_datetime_timestamp(self) -> None:
        # Datetime input is rendered to ISO 8601 with tzinfo.
        from datetime import datetime, timezone
        with _with_temp_dir(_fresh_tmp()) as tmp:
            seen_dict = {}
            url = "https://example.com/jobs/abc"
            stamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            seen.mark_seen(url, seen_dict, stamp)
            # Stored as a string in ISO format.
            self.assertEqual(seen_dict[seen._hash_key(url)], "2026-01-01T12:00:00+00:00")

    def test_mark_seen_with_none_timestamp(self) -> None:
        with _with_temp_dir(_fresh_tmp()) as tmp:
            seen_dict = {}
            url = "https://example.com/jobs/abc"
            seen.mark_seen(url, seen_dict, None)
            self.assertIsNone(seen_dict[seen._hash_key(url)])


# ---------------------------------------------------------------------------
# Helper: allocate a unique tmp dir for each test so they don't share
# a seen.json file under a parallel test runner.
# ---------------------------------------------------------------------------
def _fresh_tmp(autocreate: bool = True) -> Path:
    import tempfile
    return Path(tempfile.mkdtemp(prefix="seen-test-"))


if __name__ == "__main__":
    unittest.main()
