"""One-shot migration: idempotently add model columns missing from the
live ``public.jobs`` table.

The model in ``backend/db/models.py`` declares fields like
``company_name``, ``ats_type``, ``title``, ``ai_fit_reasoning``, etc.
that the live DB schema (applied from an earlier version of
``supabase/migrations/20260101000000_initial_schema.sql``) is missing.
This script closes the gap via ``ADD COLUMN IF NOT EXISTS`` so the
shape the SQLAlchemy ORM expects matches what Postgres has on disk.

Run once:

    python scripts/apply_jobs_alter.py

The script reads ``DATABASE_URL`` from the project-root ``.env``
(same precedence as the FastAPI lifespan in ``main.py``), strips the
``+asyncpg`` driver suffix so plain ``asyncpg`` can connect, then
issues a single ``ALTER TABLE`` statement. ``ADD COLUMN IF NOT
EXISTS`` is safe to re-run; a re-apply is a no-op once all columns
exist.

Verification output intentionally prints the post-migration column
list so the operator can sanity-check what landed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
import asyncpg


REPO_ROOT = Path(__file__).resolve().parents[2]

# Process env wins; fall back to repo-root + backend/.env (matches
# ``main._load_env_files`` precedence).
def _env(name: str) -> str:
    if name in __import__("os").environ:
        return __import__("os").environ[name]
    for env_path in (REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"):
        if env_path.is_file():
            v = dotenv_values(env_path).get(name)
            if v:
                return v
    raise RuntimeError(f"{name} not set in process env or .env files")


def _asyncpg_dsn() -> dict:
    url = urlparse(_env("DATABASE_URL").replace("+asyncpg", ""))
    return {
        "host": url.hostname,
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "database": (url.path or "/postgres").lstrip("/"),
    }


# Two statements are easier to reason about than one mixed ALTER —
# Postgres plans mixed ``ADD COLUMN`` + ``ALTER COLUMN`` sub-commands
# together but a future maintainer reads a single-column ALTER COLUMN
# without having to mentally separate the two intents.
# Three execute steps keep each ALTER shareable with future
# migrations: enums → add columns → reconcile nullability + type. A
# single mixed ALTER is harder to reason about when re-running in
# isolation against a fresh Supabase project.
ALTER = """
ALTER TABLE public.jobs
    ADD COLUMN IF NOT EXISTS company_name TEXT,
    ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS ats_type TEXT,
    ADD COLUMN IF NOT EXISTS title TEXT,
    ADD COLUMN IF NOT EXISTS url TEXT,
    ADD COLUMN IF NOT EXISTS ai_fit_score FLOAT,
    ADD COLUMN IF NOT EXISTS ai_fit_reasoning TEXT,
    ADD COLUMN IF NOT EXISTS review_deadline TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS external_id TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
"""

# The Supabase original-schema migration declared ``company_id NOT
# NULL``; the v2 model marks it ``nullable=True`` because boards
# runners land winning rows here before the outreach flow links them
# to ``companies``. ``DROP NOT NULL`` is idempotent — PG accepts it as
# a no-op when the column is already nullable, so re-runs are safe.
ALTER_COMPANY_ID_NULLABILITY = """
ALTER TABLE public.jobs
    ALTER COLUMN company_id DROP NOT NULL;
"""

# Bind ``jobs.status`` to the ``job_status`` enum type. The Supabase
# original-schema migration declared the column ``character varying``
# AND attached a DEFAULT whose expression can't be auto-cast to the
# enum, so the TYPE ALTER fails with
# ``default for column "status" cannot be cast automatically to type
# job_status``. The fix is the canonical three-step:
#
# 1. ``DROP DEFAULT`` — strip the legacy varchar-typed default.
# 2. ``TYPE x USING expression`` — bind the new type. The USING cast
#    is identity for existing rows (all 6 seed values are valid enum
#    members, scoring-service winners are always ``in_review``).
# 3. — the v2 model intentionally has *no* PG-level default; we leave
#    it that way so writers must opt-in via ``scoring_service``.
#
# All sub-commands within one ALTER TABLE statement are evaluated in
# the same step, so PG accepts the DROP DEFAULT + TYPE pair as a
# single atomic schema change. Idempotent on re-run: ``DROP DEFAULT``
# becomes a no-op when the column has no default, and the TYPE step
# evaluates ``status::job_status`` on an already-typed column as
# identity.
ALTER_STATUS_TYPE = """
ALTER TABLE public.jobs
    ALTER COLUMN status DROP DEFAULT,
    ALTER COLUMN status TYPE job_status USING status::job_status;
"""

# Idempotent enum-type reconciliation. The live DB applied the table
# DDL but never the CREATE TYPE statements from
# ``supabase/migrations/20260101000000_initial_schema.sql``, so
# Postgres can't cast ``'in_review'::job_status`` etc. Re-creating
# the types idempotently against a 16.x Supabase cluster closes the
# gap. Names + values mirror ``db/models.py`` so a future
# ``alembic check``/``alembic upgrade`` stays consistent with this
# script.
ENUM_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("company_category", ("boards", "funding", "ngos", "oss", "remote")),
    ("company_status", ("saved", "interested", "dismissed", "outreach_sent", "engaged")),
    ("job_status", ("in_review", "approved", "rejected", "applied", "flagged")),
    ("application_status", ("submitted", "interview", "rejected", "offer", "ghosted")),
    ("qa_answer_type", ("short_text", "long_text")),
    ("outreach_type", ("email", "twitter_dm", "linkedin")),
    ("scanner_kind", ("funding", "remote", "ngos", "oss", "boards")),
    ("ats_board", ("ashby", "lever", "greenhouse", "remotive", "remoteok", "hackernews")),
    ("ats_org_status", ("active", "missing", "benched")),
    ("pipeline_state", ("idle", "running", "error")),
]

# Wrap each CREATE TYPE in a ``DO $$ ... $$`` guard that checks
# ``pg_type`` first. Plain ``CREATE TYPE IF NOT EXISTS`` is **not**
# valid PostgreSQL syntax (the ``IF NOT EXISTS`` clause is supported
# for tables / indexes / schemas / sequences / extensions, but not
# for custom enum types). Doing the guard client-side keeps the
# script idempotent: re-runs early-exit on the existence check
# without raising ``duplicate_object``.
ENUM_DDL_GUARDED = (
    "DO $$\n"
    "BEGIN\n"
    + "\n".join(
        "    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{n}') THEN\n"
        "        CREATE TYPE {n} AS ENUM ({v});\n"
        "    END IF;".format(
            n=name,
            v=", ".join(f"'{x}'" for x in values),
        )
        for name, values in ENUM_TYPES
    )
    + "\nEND $$;"
)


async def main() -> None:
    dsn = _asyncpg_dsn()
    print(f"Connecting to {dsn['host']}:{dsn['port']}/{dsn['database']} as {dsn['user']}")
    conn = await asyncpg.connect(**dsn)
    try:
        print("Applying CREATE TYPE (guarded) for all 10 enums …")
        await conn.execute(ENUM_DDL_GUARDED)
        print("Enums reconciled.")
        print("Applying ALTER TABLE …")
        await conn.execute(ALTER)
        print("ALTER TABLE succeeded.")
        print("Reconciling jobs.company_id nullability …")
        await conn.execute(ALTER_COMPANY_ID_NULLABILITY)
        print("company_id is now nullable.")
        print("Binding jobs.status to job_status enum …")
        await conn.execute(ALTER_STATUS_TYPE)
        print("jobs.status type reconciled.")
        types = await conn.fetch(
            """
            select t.typname,
                   array_agg(e.enumlabel order by e.enumsortorder) as values
            from pg_type t
            join pg_enum e on t.oid = e.enumtypid
            where t.typname in (
                'company_category', 'company_status', 'job_status',
                'application_status', 'qa_answer_type', 'outreach_type',
                'scanner_kind', 'ats_board', 'ats_org_status', 'pipeline_state'
            )
            group by t.typname
            order by t.typname
            """
        )
        print("\nPostgres enum types reconciled:")
        for t in types:
            print(f"  {t['typname']}: {list(t['values'])}")

        cols = await conn.fetch(
            """
            select column_name || ' ' || data_type ||
                   case when is_nullable='NO' then ' NOT NULL' else '' end
            from information_schema.columns
            where table_schema='public' and table_name='jobs'
            order by ordinal_position
            """
        )
        print("\nFinal public.jobs columns:")
        for c in cols:
            print(f"  {c[0]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
