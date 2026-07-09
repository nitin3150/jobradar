"""One-shot Supabase row-count probe.

Diagnostic helper for the "where did my rows go?" debugging path. Loads
``.env`` from the repo root, parses ``DATABASE_URL`` via ``urllib`` (no
shell escaping), runs ``psql`` with cleanly-separated ``-h/-p/-U/-d``
arguments (the bash heredoc variants have bitten us), and prints only
non-sensitive results — counts + a project-ref sanity check between
``DATABASE_URL`` and ``SUPABASE_URL``.

Optional Step 1+2 (``RENDER_URL`` reachable + GET ``/api/jobs/pending-count``
+ GET ``/api/jobs?page_size=200``) only fires if ``RENDER_URL`` is exported
in the shell. Without it, only the Supabase-side counts run — usually
enough to nail the "is the table really empty?" question.

Run from the repo root:

    python scripts/verify_jobs.py

Exit code 0 always — this is a read-only diagnostic, not a CI gate.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(*paths: Path) -> None:
    """Tiny Naive ``.env`` loader — handles ``KEY=VALUE`` and
    ``export KEY=VALUE``, strips surrounding double/single quotes,
    ignores comments and blank lines. Iterates the supplied paths in
    order so callers can pass ``(root / ".env", root / "backend" /
    ".env")`` and mirror :func:`scripts.apply_jobs_alter._env`'s
    precedence.

    Deliberately avoids pulling in ``python-dotenv`` just for this —
    the script stays a single file with no third-party deps. Existing
    instance values (already in ``os.environ``) win over the file so a
    shell-exported ``RENDER_URL`` overrides any stale ``.env`` copy.
    First-file-wins on duplicates so a developer override at the root
    isn't silently rewritten by a vendored ``backend/.env``.
    """
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


# Read order matches scripts/apply_jobs_alter.py convention:
# process env wins; root ``.env`` first; ``backend/.env`` fallback.
_load_dotenv(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env")


def _parse_supabase_ref(raw: str) -> str:
    """Pull the project ref (``abcdefghijk``) out of ``SUPABASE_URL``
    regardless of whether the value carries ``/rest/v1/`` or a
    trailing slash.
    """
    out = urllib.parse.urlparse(raw.strip())
    host = out.netloc or out.path
    if host.endswith(".supabase.co"):
        return host.split(".", 1)[0]
    return ""


def _row_counts_via_psql(dsn_plain: str) -> dict[str, int] | None:
    """Run psql with separated args so ``-h`` is unambiguous. Returns
    ``None`` on connection failure so the caller can decide whether to
    continue or surface the error verbatim.
    """
    parsed = urllib.parse.urlparse(dsn_plain)
    db_user = parsed.username or ""
    db_password = parsed.password or ""
    db_host = parsed.hostname or ""
    db_port = str(parsed.port or 5432)
    db_name = (parsed.path or "/postgres").lstrip("/")
    if not (db_host and db_user):
        print("  ERROR: DATABASE_URL didn't yield host+user when parsed.",
              file=sys.stderr)
        return None
    if not shutil.which("psql"):
        print("  ERROR: `psql` not found on PATH. Install the",
              "Postgres client tools or use the `psycopg2-binary`",
              "fallback path.", file=sys.stderr)
        return None

    # ``count(*)`` is exact — the alternative ``pg_stat_user_tables.n_live_tup``
    # is the autovacuum-analyzed estimate and reports 0 for tables that no
    # ANALYZE has touched yet (common right after a wipe + bulk insert, which
    # is exactly when the operator reaches for this script). A single
    # ``count(*)`` per table is sub-100 ms at the 2K-row scale JobRadar deals
    # with; trade the speed for ground truth.
    sql = (
        "select 'jobs' as table_name, count(*) as row_count from public.jobs "
        "union all select 'job_status_history', count(*) from public.job_status_history "
        "union all select 'research_reports', count(*) from public.research_reports "
        "union all select 'companies', count(*) from public.companies "
        "union all select 'applications', count(*) from public.applications "
        "order by 1"
    )
    env = {**os.environ, "PGPASSWORD": db_password}
    try:
        out = subprocess.run(
            [
                "psql",
                "-h", db_host,
                "-p", db_port,
                "-U", db_user,
                "-d", db_name,
                "-t", "-A",
                "-F", "|",
                "-c", sql,
            ],
            env=env, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  ERROR: psql timed out after 30 s.", file=sys.stderr)
        return None
    if out.returncode != 0:
        # Surface the FIRST stderr line + the FAILED query so the
        # operator can grep their worker logs for the same string.
        first = (out.stderr or "").strip().splitlines()[:1]
        print(f"  ERROR: psql rc={out.returncode}: {first[0] if first else '(no stderr)'}",
              file=sys.stderr)
        return None

    counts: dict[str, int] = {}
    for line in out.stdout.splitlines():
        if "|" not in line:
            continue
        tbl, _, cnt = line.strip().partition("|")
        try:
            counts[tbl] = int(cnt)
        except ValueError:
            continue
    return counts


def _curl_json(url: str, timeout: float = 10.0) -> dict | list | None:
    """Best-effort GET + JSON parse via curl subprocess. Returns
    parsed body or ``None`` on failure.
    """
    if not shutil.which("curl"):
        return None
    try:
        out = subprocess.run(
            [
                "curl", "-sS", "--max-time", str(int(timeout)),
                "-H", "Accept: application/json",
                url,
            ],
            capture_output=True, text=True, timeout=timeout + 1,
        )
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def main() -> int:
    print("=" * 60)
    print(" JobsRadar — Supabase row-count verifier")
    print("=" * 60)
    print()

    # ----------------------------------------------------------------
    # Step 0 — DSN parse + project-ref sanity check
    # ----------------------------------------------------------------
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL not set in shell or .env.", file=sys.stderr)
        return 1
    dsn_plain = dsn.replace("+asyncpg", "")

    parsed = urllib.parse.urlparse(dsn_plain)
    db_user = parsed.username or ""
    db_host = parsed.hostname or ""
    db_port = parsed.port or 5432
    db_name = (parsed.path or "/postgres").lstrip("/")

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_ref = _parse_supabase_ref(supabase_url)

    print("DATABASE_URL host     :", db_host)
    print("DATABASE_URL user     :", db_user)
    print("DATABASE_URL port     :", db_port)
    print("DATABASE_URL db       :", db_name)
    print("SUPABASE_URL          :", supabase_url or "(unset)")
    print("SUPABASE project ref  :", supabase_ref or "(unparseable)")

    if db_host and ".pooler.supabase.com" in db_host:
        pooler_hint = "transaction/connection pooler (port 6543)"
    elif db_host and ".supabase.com" in db_host and "pooler" not in db_host:
        pooler_hint = "direct connection hostname"
    else:
        pooler_hint = "non-Supabase hostname"
    print("DATABASE_URL connection:", pooler_hint)
    print()

    # Project-ref match is interpretive — newer pooler hostnames embed
    # the project ref, older pooler hostnames are region-shared.
    if supabase_ref:
        ref_in_host = supabase_ref in db_host
        print(f"Project-ref '{supabase_ref}' in DB host? ", end="")
        print("YES" if ref_in_host else "NO (older shared pooler — fine)")
    print()

    # ----------------------------------------------------------------
    # Step 3 — Supabase row counts
    # ----------------------------------------------------------------
    print("[STEP 3] psql row counts (public schema, 5 key tables)")
    print("-" * 60)
    counts = _row_counts_via_psql(dsn_plain)
    if counts is None:
        print("  (no counts — see errors above)")
    else:
        target_tables = ("jobs", "job_status_history", "research_reports",
                         "companies", "applications")
        for tbl in target_tables:
            n = counts.get(tbl, "(missing — not in pg_stat_user_tables)")
            print(f"  public.{tbl:<22s}  {n}")
    print()

    # ----------------------------------------------------------------
    # Step 1+2 — Render backend (only if RENDER_URL is set)
    # ----------------------------------------------------------------
    render_url = os.environ.get("RENDER_URL", "").strip().rstrip("/")
    if render_url:
        print("[STEP 1+2] Render backend probes")
        print("-" * 60)
        print(f"  GET {render_url}/api/jobs/pending-count")
        body = _curl_json(f"{render_url}/api/jobs/pending-count")
        if isinstance(body, dict) and "count" in body:
            print(f"     -> count = {body['count']}")
        else:
            print(f"     -> (no JSON count; raw body: {shlex.quote(str(body)[:120])})")

        print(f"  GET {render_url}/api/jobs?page_size=200")
        body = _curl_json(f"{render_url}/api/jobs?page_size=200")
        if isinstance(body, dict) and "jobs" in body:
            total = body.get("total", "?")
            returned = len(body.get("jobs") or [])
            first_three = [j.get("title") for j in (body.get("jobs") or [])[:3]]
            print(f"     -> total={total}  returned={returned}  "
                  f"first_three_titles={first_three or '[]'}")
        else:
            print(f"     -> (no JSON jobs array; raw body: {shlex.quote(str(body)[:120])})")
        print()
    else:
        print("[STEP 1+2] Render backend probes — SKIPPED")
        print("  (export RENDER_URL=https://<your-service>.onrender.com to enable)")
        print()

    # ----------------------------------------------------------------
    # Final read-out
    # ----------------------------------------------------------------
    print("[SUMMARY]")
    print("-" * 60)
    if counts:
        jobs_n = counts.get("jobs", 0)
        history_n = counts.get("job_status_history", 0)
        if jobs_n == 0:
            print(f"  jobs table is EMPTY ({jobs_n} rows).")
        else:
            print(f"  jobs table has {jobs_n} rows.")
        print(f"  {history_n} job_status_history rows (audit-trail activity).")
    if supabase_ref and db_host:
        print(f"  Database host '{db_host}' is the same project as "
              f"SUPABASE_URL (ref {supabase_ref})."
              if supabase_ref in db_host
              else f"  Note: DB host '{db_host}' does not contain the "
                   f"Supabase project ref — older region-shared pooler "
                   f"layout, project isolation is via the user "
                   f"('{db_user}') instead.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
