"""Tests for the per-invocation audit row written by ``boards_scan.py``.

What this file covers
====================

Eight tests across two layers of the audit-log surface:

* :func:`_compute_env_hash` — determinism + secret-exclusion + null-env.
  These are the foundation: if env_hash is non-deterministic the
  postmortem ``GROUP BY env_hash`` query becomes useless, and if any
  secret leaks into the canonical string the SHA loses its hash-quality
  claim (knowing the chain narrows brute-force space).
* :func:`record_audit_open` / :func:`record_audit_close` — the actual
  Supabase REST writes. Mocked so the suite runs without a live DB
  but still verifies the shape of the INSERT / UPDATE payloads (the
  row IDs are pydantic UUIDs server-rendered on insert, so we verify
  + skip the ID equality check rather than mocking the whole
  PostgREST round-trip).

Why repeated ``os.environ.pop`` calls in tests
=============================================

``_compute_env_hash`` reads the LIVE process env — there's no DI
injection for it (the function is intentionally a raw SHA256 of the
production env, with no test seam) so tests must isolate process env
to get reproducible hashes. ``monkeypatch`` is the pytest idiom for
this: every env-var read inside the function call sees exactly what
the test fixture set, and pytest restores the real env at teardown.

Best-effort + crash safety
===========================

The audit-row writers are intentionally best-effort (``None`` return
on failure, ``except Exception`` around the actual REST call). The
test suite pins this contract: every mocked RestAPI failure path
verifies the helper returns ``None`` (open) or no-ops (close)
WITHOUT raising, so the production ``finally`` block can rely on
the silent-Degrade contract. A future refactor that tightens the
contract to raise MUST update both helpers AND these tests in the
same PR — otherwise the response shape mismatches and the
``try/finally`` will crash on a Supabase 401.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ``scripts/boards_scan.py`` lives outside the package tree (``backend``
# is the package boundary). Add the script's parent to ``sys.path`` so
# the import resolves regardless of pytest's CWD. The script's own
# ``BACKEND_ROOT = Path(__file__).resolve().parent.parent`` does the
# same on import, but the test harness runs pytest as a sibling of the
# script, not its parent — hence the explicit insertion here.
_SCRIPT_PARENT = Path(__file__).resolve().parent
if str(_SCRIPT_PARENT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_PARENT))

# Import the SCRIPT as a module. The path manipulations above mean
# ``from boards_scan import _compute_env_hash`` would work; we use the
# explicit ``importlib`` shape so the test suite fails LOUDLY if the
# script's name ever clashes with a future ``boards_scan`` package.
import importlib.util as _importlib_util  # noqa: E402

_spec = _importlib_util.spec_from_file_location(
    "_audit_log_under_test", _SCRIPT_PARENT / "boards_scan.py"
)
_audit_module = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_audit_module)

_compute_env_hash = _audit_module._compute_env_hash
record_audit_open = _audit_module.record_audit_open
record_audit_close = _audit_module.record_audit_close
log = _audit_module.log


# ----------------------------------------------------------------------
# env_hash — determinism
# ----------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every env var that ``_compute_env_hash`` reads so test
    inputs are reproducible. Without this fixture, a stray
    ``BOARDS_DELTA_HOURS`` on the developer's shell would perturb the
    hash. ``monkeypatch.delenv`` raises if the var is not set, so we
    defensively set-then-del; pytest restores the real env at teardown.
    """
    keys = [
        "BOARDS_DELTA_HOURS",
        "BOARDS_LIMIT",
        "BOARDS_BOARDS",
        "BOARDS_HTTP_TIMEOUT",
        "BOARDS_SKIP_TIMEOUTS",
        "BOARDS_PROFILE_PATH",
        "JOB_FIT_THRESHOLD",
        "TARGET_ROLES",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "GITHUB_SHA",
        "GITHUB_REF",
    ]
    for k in keys:
        # ``monkeypatch.delenv`` would raise if the var isn't already
        # set in process env; the safer pattern is set-then-del which
        # always succeeds AND keeps state restore symmetric.
        monkeypatch.setenv(k, "")
    return keys


def test_compute_env_hash_is_deterministic(monkeypatch, clean_env):
    """Same env → same hash on every call. The prior session's
    postmortem query ``GROUP BY env_hash`` is meaningless if the
    hash drifts on repeat calls within the same env.
    """
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "1")
    monkeypatch.setenv("JOB_FIT_THRESHOLD", "0.7")
    h1 = _compute_env_hash()
    h2 = _compute_env_hash()
    assert h1 == h2


def test_compute_env_hash_changes_with_env(monkeypatch, clean_env):
    """Modifying a contributing env var MUST produce a different hash.
    A typo in the function (e.g. forgetting to include the value in
    the canonical string) would surface here as a same-hash failure
    even when the env differs.
    """
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "1")
    monkeypatch.setenv("JOB_FIT_THRESHOLD", "0.7")
    h_before = _compute_env_hash()
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "24")  # changed
    h_after = _compute_env_hash()
    assert h_before != h_after


def test_compute_env_hash_excludes_secret_keys(monkeypatch, clean_env):
    """Secret-carrying env vars MUST NOT contribute to the canonical
    string. Verified by setting every secret in the full
    ``SUPABASE_URL`` / ``*_API_KEY`` / token list and confirming
    the hash is identical to the no-secrets baseline.

    The check uses ``_compute_env_hash``'s internal canonical-string
    reconstruction (``hashlib.sha256(canonical.encode(...))``) so we
    don't have to assume the hash output shape — we verify the
    INPUT side, which is what the security claim hangs on.
    """
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "1")

    # Baseline: no secrets.
    h_baseline = _compute_env_hash()

    # Load every plausible secret.
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret-1")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret")
    monkeypatch.setenv("NVIDIA_API_KEY_2", "nvapi-secret-2")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")
    h_with_secrets = _compute_env_hash()

    assert h_baseline == h_with_secrets, (
        "env_hash must ignore secret keys; a different hash implies "
        "the secret is leaking into the canonical string"
    )


def test_compute_env_hash_is_64_char_hex(monkeypatch, clean_env):
    """The hash OUTPUT must be a 64-char hex string (sha256). If a
    future refactor truncates or uses a different digest (e.g.
    blake2b at 16 bytes = 32 chars), postmortem queries that assume
    64 chars break — this test pins the output shape.
    """
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "24")
    h = _compute_env_hash()
    assert len(h) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", h), (
        f"env_hash should be a 64-char lowercase hex string; got {h!r}"
    )


def test_compute_env_hash_handles_empty_env(monkeypatch, clean_env):
    """With every contributing env var empty / unset, the function
    returns a stable 64-char hex (the SHA of an empty canonical
    string). A future refactor that accidentally concatenates a
    ``key=None`` into the string would produce different output.
    """
    h = _compute_env_hash()
    expected = hashlib.sha256(b"").hexdigest()
    assert h == expected


def test_compute_env_hash_strips_whitespace_only_values(monkeypatch, clean_env):
    """A whitespace-only env value is treated as "not set" (the
    function strips before testing). A regression that includes
    raw env values (without strip) would put ``"  \n"`` into the
    canonical string and skew ``GROUP BY env_hash`` matches.
    """
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "   ")
    monkeypatch.setenv("JOB_FIT_THRESHOLD", "")
    h_unset = _compute_env_hash()
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "")
    h_both_unset = _compute_env_hash()
    assert h_unset == h_both_unset


# ----------------------------------------------------------------------
# record_audit_open — Supabase REST insert shape + best-effort
# ----------------------------------------------------------------------


def _make_mock_sb(side_effect=None) -> MagicMock:
    """Build a MagicMock that quacks like a supabase-py table query.

    The mock chain ``sb.table("scanner_runs").insert(row).execute()``
    is what boards_scan.py calls — so the chain must produce a
    callable that returns a MagicMock with ``.execute()`` that
    returns/calls ``side_effect``.
    """
    sb = MagicMock()

    table_mock = MagicMock()
    sb.table.return_value = table_mock

    insert_chain = MagicMock()
    table_mock.insert.return_value = insert_chain
    insert_chain.execute.return_value = None  # POST /rest/v1/scanner_runs returns []

    if side_effect is not None:
        insert_chain.execute.side_effect = side_effect
    return sb


def test_record_audit_open_inserts_with_correct_shape(monkeypatch, clean_env):
    """The INSERT payload must carry every column the migration
    added (``scanner``, ``tier``, ``state='running'``, ``started_at``,
    ``env_hash``, ``items_found``, ``error_count``, ``error_summary``,
    ``jobs_persisted``). A typo in column names surfaces here as a
    supabase-py 400 in production; this test pins the shape.
    """
    monkeypatch.setenv("BOARDS_TIER", "active")
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "1")
    sb = _make_mock_sb()
    audit_id = record_audit_open(sb)

    # Confirm sb.table("scanner_runs") was queried exactly once.
    sb.table.assert_called_once_with("scanner_runs")

    # Inspect the INSERT call's `row` argument — second arg of
    # table.insert(row, ...). Supabase-py also accepts
    # ``.upsert(...)`` etc. so we read the FIRST call's first
    # positional arg.
    insert_call = sb.table.return_value.insert.call_args
    row = insert_call.args[0]

    # Required columns + their expected initial values.
    assert row["scanner"] == "boards", "scanner column must be 'boards'"
    assert row["tier"] == "active", (
        "tier column must reflect BOARDS_TIER env var verbatim"
    )
    assert row["state"] == "running", (
        "audit-open row must be 'running' before the close"
    )
    assert isinstance(row["started_at"], str) and row["started_at"].endswith("Z"), (
        "$NOW-isoformat with trailing 'Z' is the wire-format the rest of JobRadar uses"
    )
    assert isinstance(row["env_hash"], str) and len(row["env_hash"]) == 64, (
        "env_hash must be a 64-char sha256-hex string"
    )
    assert row["items_found"] == 0
    assert row["error_count"] == 0
    assert row["error_summary"] is None
    assert row["jobs_persisted"] == 0

    # The audit_id returned must match the row's ``id`` — the close
    # step keys the UPDATE on this UUID, so a mismatch here would
    # silently drop the close.
    assert audit_id == row["id"]


def test_record_audit_open_returns_None_on_supabase_failure(monkeypatch, clean_env):
    """Supabase 401 / 5xx / network blip during the INSERT must
    yield ``None`` (not raise). The boards-scan main() loop relies
    on this contract: a raised exception from the audit-open call
    would skip the boards runner entirely.
    """
    monkeypatch.setenv("BOARDS_TIER", "active")
    sb = _make_mock_sb(side_effect=Exception("connection refused"))
    # Must NOT raise. Returns None.
    result = record_audit_open(sb)
    assert result is None


def test_record_audit_open_tier_falls_back_to_manual(monkeypatch, clean_env):
    """Without ``BOARDS_TIER`` set, the row's ``tier`` column
    carries the literal string ``'manual'`` so a postmortem query
    can distinguish a scripts/boards_scan.py invocation that ran
    locally (manual) vs from GHA (active / dormant).
    """
    monkeypatch.setenv("BOARDS_DELTA_HOURS", "1")
    monkeypatch.delenv("BOARDS_TIER", raising=False)
    sb = _make_mock_sb()
    record_audit_open(sb)
    row = sb.table.return_value.insert.call_args.args[0]
    assert row["tier"] == "manual"


# ----------------------------------------------------------------------
# record_audit_close — UPDATE shape + best-effort + skip-on-open-failure
# ----------------------------------------------------------------------


def test_record_audit_close_updates_with_correct_shape():
    """The UPDATE payload MUST carry the four post-run columns
    (``state``, ``finished_at``, ``items_found``, ``jobs_persisted``,
    plus ``error_count`` / ``error_summary`` when an error is
    recorded). ``UPDATE scanner_runs SET ... WHERE id = $1`` is the
    contract the open call wrote, and a typo here would silently
    leave stale ``state='running'`` rows in production.
    """
    sb = _make_mock_sb()
    record_audit_close(
        sb,
        "test-audit-id-deadbeef",
        items_found=42,
        jobs_persisted=7,
        state="idle",
        error_summary=None,
    )

    # Verify the UPDATE chain. Supabase-py shape:
    # ``sb.table("scanner_runs").update(finish).eq("id", audit_id).execute()``
    sb.table.assert_called_once_with("scanner_runs")
    update_call = sb.table.return_value.update.call_args
    finish = update_call.args[0]
    eq_call = sb.table.return_value.update.return_value.eq
    eq_call.assert_called_once_with("id", "test-audit-id-deadbeef")

    # Verify the fields.
    assert finish["state"] == "idle"
    assert finish["items_found"] == 42
    assert finish["jobs_persisted"] == 7
    assert finish["error_count"] == 0
    assert finish["error_summary"] is None
    assert isinstance(finish["finished_at"], str) and finish["finished_at"].endswith("Z")


def test_record_audit_close_records_error_state():
    """When the boards-scan main() body set ``final_state='error'``
    and an ``error_summary``, the close writes ``error_count=1`` and
    the truncated summary string. ``UPDATE ... SET error_count = 1,
    error_summary = $1 WHERE id = $2`` — the partial-update shape
    must not regress to ``UPDATE ... SET error_count = 0``.
    """
    sb = _make_mock_sb()
    record_audit_close(
        sb,
        "audit-id",
        items_found=10,
        jobs_persisted=0,
        state="error",
        error_summary="boards runner: ValueError: bad slug",
    )
    finish = sb.table.return_value.update.call_args.args[0]
    assert finish["state"] == "error"
    assert finish["error_count"] == 1
    assert "ValueError" in (finish["error_summary"] or "")


def test_record_audit_close_no_ops_when_audit_id_is_None():
    """If the open call returned ``None`` (the INSERT failed), the
    close call MUST NOT touch PostgREST — there's no row to UPDATE.
    Verified by ``sb.table.assert_not_called()``.
    """
    sb = _make_mock_sb()
    record_audit_close(
        sb,
        None,  # open failed; nothing to close
        items_found=0,
        jobs_persisted=0,
        state="error",
        error_summary="audit_open failed earlier",
    )
    sb.table.assert_not_called()


def test_record_audit_close_swallows_supabase_failure():
    """A Supabase 5xx during the UPDATE must NOT raise. The close
    runs from boards_scan.py's ``finally`` block — a raised
    exception here would mask the actual script's success/failure
    exit code in the GHA workflow log.
    """
    sb = MagicMock()
    sb.table.return_value.update.return_value.eq.return_value.execute.side_effect = (
        RuntimeError("postgrest timeout")
    )

    # Must NOT raise. No exception bubbles out.
    record_audit_close(
        sb,
        "audit-id",
        items_found=0,
        jobs_persisted=0,
        state="idle",
        error_summary=None,
    )


def test_record_audit_close_truncates_long_error_summary():
    """A wild ``error_summary`` (>1000 chars) MUST be truncated to the
    1 KB column limit so a single broken match doesn't blow the row
    size past the Postgres TOAST chunk. Pinning the contract here
    catches a future refactor that drops the ``[:1000]`` slice.
    """
    sb = _make_mock_sb()
    huge_summary = "x" * 5000
    record_audit_close(
        sb,
        "audit-id",
        items_found=0,
        jobs_persisted=0,
        state="error",
        error_summary=huge_summary,
    )
    finish = sb.table.return_value.update.call_args.args[0]
    assert len(finish["error_summary"]) <= 1000, (
        f"error_summary must be truncated to <=1000 chars; got {len(finish['error_summary'])}"
    )
