-- =============================================================================
-- Boards-scan audit metadata on ``scanner_runs`` — GHA cron postmortem trail.
-- =============================================================================
--
-- Why this migration exists
-- --------------------------
-- The operator lost ~300 review-queue rows to a silent env swap; every GHA
-- cron tick since then wrote to a fresh (empty) Supabase project, the React
-- page rendered 12 jobs because the DB genuinely had 12 rows, and the only
-- postmortem trail was the GHA run log (per-run_id, ephemeral). Long-term
-- attribution requires audit rows that live INSIDE the database — closer
-- to the data they record — so ``boards_scan.py`` writes one row per
-- invocation and closes it on success / failure / crash.
--
-- Sync contract
-- -------------
-- This file MUST stay byte-equivalent to the Alembic mirror at
-- ``backend/db/migrations/versions/0005_scanner_runs_boards_metadata.py``.
-- Drift is a silent-deployed-DDL bug. See
-- ``20260101000000_initial_schema.sql``'s comment block for the full rule.
--
-- Apply:    supabase db push    (from the repo root)
-- Rollback: supabase db reset   (drops + re-applies; manual rollback not in
--           scope — see the Alembic downgrade for the DDL-only rollback).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 5.1 Three additive nullable columns on ``scanner_runs``.
--
-- NULL-able (not server_defaulted) so the funding / remote / ngos / oss
-- scanners can leave them NULL — only boards-scan writes them. The
-- boards-scan writer in ``scripts/boards_scan.py`` fills them in a
-- try/finally so a crash mid-scan still produces a ``state=error``
-- audit row that points back to the exact env-hash + tier.
-- -----------------------------------------------------------------------------
ALTER TABLE scanner_runs ADD COLUMN IF NOT EXISTS tier            TEXT NULL;
ALTER TABLE scanner_runs ADD COLUMN IF NOT EXISTS env_hash        TEXT NULL;
ALTER TABLE scanner_runs ADD COLUMN IF NOT EXISTS jobs_persisted  INTEGER NULL;


-- -----------------------------------------------------------------------------
-- 5.2 Per-tier history index.
--
-- Supports the operator's first-question query after a postmortem:
-- ``SELECT ... WHERE tier = 'active' ORDER BY started_at DESC LIMIT 50``.
-- (tier, started_at DESC) means the tier filter alone hits a partial
-- scan; ORDER BY reuses the index ordering so the index-only-scan is
-- non-sorting.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_scanner_runs_tier_started
    ON scanner_runs (tier, started_at DESC);
