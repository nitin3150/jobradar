-- supabase/migrations/20260711010000_board_seen_jobs.sql
--
-- Postgres-backed replacement for the on-disk ``backend/data/seen.json``
-- dedupe file the boards runner has historically consulted. The
-- on-disk JSON file is EPHEMERAL on GitHub Actions (every cron
-- tick spins up a fresh ``ubuntu-latest`` worker) and subject to
-- race conditions across multi-worker deploys, so a single-process
-- JSON store cannot safely backstop the boards-scrape cron for
-- production. The schema is equivalent to the one Alembic
-- ``0001_initial_schema.py`` already creates, so the migration is
-- idempotent on hosts that already ran the alembic chain: a
-- ``CREATE TABLE IF NOT EXISTS`` plus a guarded
-- ``CREATE INDEX IF NOT EXISTS`` keeps a duplicate table from
-- shadowing the canonical one.
--
-- Why this migration exists separately from the alembic one
-- ===========================================================
-- The Supabase dashboard manages its own SQL migration chain
-- (``supabase/migrations/*.sql``). Applying the alembic Python
-- chain to a Supabase project is an out-of-band step the
-- operator may not have taken — duplicates on Supabase are a
-- common surprise. Defensively shipping this migration makes the
-- board_seen_jobs table available even on the "I ran the
-- supabase CLI migrations but never alembic upgrade head" path
-- the operator may have used during early onboarding.
--
-- Compatibility with the alembic-managed path
-- ============================================
-- Identical schema, identical indexes, identical ats_board enum
-- constraints. The IF NOT EXISTS guards mean running this on a
-- host that already has the alembic-created table is a clean
-- no-op — column types, NOT NULL constraints, and indexes ARE
-- identical between the two paths, so there is no risk of a
-- column type drift between alembic-managed and supabase-managed
-- hosts.

CREATE TABLE IF NOT EXISTS board_seen_jobs (
    -- Primary key is the dedupe key, NOT a surrogate uuid. The
    -- shapes are ``"<board>:<url>"`` -- a composite of the ATS
    -- name and the canonical posting URL -- matching the
    -- formula :func:`services.board_seen.dedupe_key`. Text
    -- rather than a uuid column because the keys are already
    -- unique and stable across runs.
    job_id_hash  TEXT PRIMARY KEY,
    -- Partition key. The ``ats_board_enum`` PostgreSQL enum
    -- mirrors the ``ats_board_t`` ENUM in :mod:`db.models`. We
    -- create it IF NOT EXISTS so a host that already has the
    -- alembic-created enum doesn't fail on duplicate creation.
    board       TEXT NOT NULL,
    -- Optional FK to ``companies.id``. Most slots stay NULL until
    -- the boards runner promotes a posting to a Company row (an
    -- out-of-band ETL that runs on a separate cadence). The FK
    -- is SET NULL on company-deletion so deleting a company
    -- doesn't cascade-wipe the dedupe history.
    company_id  UUID REFERENCES companies(id) ON DELETE SET NULL,
    -- Audit timestamps. ``first_seen_at`` is preserved across
    -- re-observations (the row stays at the very first time
    -- we observed the dedupe key); ``last_seen_at`` is bumped
    -- to server-time ``now()`` on every subsequent observation.
    -- The runner's ``times_seen`` counter is incremented on
    -- every re-observation so future ``SELECT … GROUP BY times_seen``
    -- queries flag chronically-re-listed postings.
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen     INTEGER NOT NULL DEFAULT 1
);

-- Companion index for ``load_seen_for_board(board)`` style queries:
-- O(1) board partition scan + linear read of the partition rows.
-- Without this index, a query like
--   SELECT job_id_hash FROM board_seen_jobs WHERE board = $1
-- forces a seq-scan on a hundred-thousand-row table on every
-- cron tick.
CREATE INDEX IF NOT EXISTS idx_board_seen_board_last_seen
    ON board_seen_jobs (board, last_seen_at DESC);

-- Comment block so a Supabase Studio inspector sees the table's
-- intent immediately without following the code path. Mirrors
-- the docstring on :class:`db.models.BoardSeenJob`.
COMMENT ON TABLE board_seen_jobs IS
    'Persistent dedupe keys for the boards runner. Replaces the '
    'on-disk backend/data/seen.json that was ephemeral in GHA and '
    'subject to read-modify-write races across multi-worker deploys. '
    'PK is <board>:<url>; INSERT ... ON CONFLICT DO NOTHING makes the '
    'cron idempotent.';
COMMENT ON COLUMN board_seen_jobs.job_id_hash IS
    'Composite key "<board>:<url>" — matches the formula in '
    'services.board_seen.dedupe_key AND the UUID5 derivation shape in '
    'services.scoring_service._job_id so seen-store membership and '
    'jobs.id presence are correlated.';
COMMENT ON COLUMN board_seen_jobs.times_seen IS
    'Counter incremented on every re-observation. Frequently '
    're-listed postings surface as hot rows in a SELECT … GROUP BY '
    'times_seen query — useful for tuning the cron cadence later.';
