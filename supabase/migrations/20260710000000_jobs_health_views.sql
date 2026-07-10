-- =============================================================================
-- jobs_health_views — diagnostic views for the Supabase ``jobs`` table.
-- =============================================================================
--
-- Why this migration exists
-- --------------------------
-- The operator lost a Supabase project's worth of ~300 review-queue rows to
-- a silent env swap (Supabase URL secret rotated to a fresh project after a
-- GHA deploy) — every cron tick since then wrote to the new (empty) project;
-- the React page rendered 12 jobs because the DB genuinely had 12 rows; the
-- default route filter was correctly returning everything; and the operator had
-- no easy surface in the Supabase SQL Editor to say "wait — there should be
-- 300 here, the last older row should be 3 weeks old, why is everything 1
-- hour old?". This migration installs that surface.
--
-- Two views, both pure-select over ``jobs`` (no triggers, no extra tables, no
-- background jobs), so they survive every Render restart + Supabase failover
-- without maintenance. The group-by keys are exactly the three dimensions
-- the operator wants at-a-glance: ``ats_type``, ``status``, hour-bucket of
-- ``created_at``. The views are deliberately simple (no joins) so even when
-- the operator accidentally points them at a mis-migrated DB they still
-- return a clean shape.
--
-- Sync contract
-- -------------
-- Any change here MUST be mirrored to the Alembic migration
-- ``backend/db/migrations/versions/0004_jobs_health_views.py``. The Supabase
-- CLI applies this file to the remote project; Alembic applies the Python
-- mirror on Render's local lifespan boot. Drift between the two is a
-- silent-deployed-DDL bug — see the in-line comments in 0001_initial_schema.sql
-- for the full rationale.
--
-- Apply:    supabase db push    (from the repo root)
-- Rollback: supabase db reset  (drops + re-applies; manual rollback not in
--           scope — see the Alembic downgrade for the DDL-only rollback).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 4.1 jobs_health_summary — at-a-glance total per (ats_type, status).
--
-- The "first query" the operator runs once the React page count looks wrong.
-- A board missing from this view, a status count that flipped from 280 to 0,
-- or a row total that's 10x smaller than expected — all surface in one
-- 6-row result. By design we DO NOT bucket by hour here so the operator
-- sees the cumulative shape ("30 approved, 12 in_review, 240 applied" reads
-- the same regardless of when those rows landed), and the timeseries view
-- below carries the time axis.
--
-- The ``COALESCE`` on ``status`` defends against a future unscored row
-- landing with ``status IS NULL`` (today's ``job_status_t`` enum is
-- ``NOT NULL`` so this is unused defensive benefit, but the cost of
-- keeping the guard total is one COALESCE per scan).
-- -----------------------------------------------------------------------------
CREATE VIEW jobs_health_summary AS
SELECT
    ats_type,
    COALESCE(status, '(unknown)') AS status,
    COUNT(*)                       AS row_count,
    MIN(created_at)                AS oldest_row_created_at,
    MAX(created_at)                AS newest_row_created_at,
    MAX(updated_at)                AS last_touched_at
FROM jobs
GROUP BY ats_type, status
ORDER BY ats_type, status;


-- -----------------------------------------------------------------------------
-- 4.2 jobs_health_timeseries — hour-bucketed (ats_type, status) counts.
--
-- The "second query" — once the summary tells the operator a board/status is
-- missing, the timeseries shows WHEN the rows landed. An env swap shows up
-- as a single tight time cluster (every row created_at within 1 hour of the
-- swap). A destructive migration shows up as a gap (rows before T, no rows
-- at T, rows resume at T+epsilon). A clean DB shows a roughly-flat histogram.
--
-- ``DATE_TRUNC('hour', created_at)`` is the pre-existing granularity the
-- React frontend already aggregates on in its dashboard — picking a
-- coarser or finer bucket would create SQL/UI parity drift the operator
-- would notice ("the SQL says 3 buckets, the dashboard says 7"). Index-
-- friendly: an eventual ``idx_jobs_created_at`` would cover this query;
-- we don't add one now because the table is <500 rows and the operator
-- runs this query by hand, not on the request path.
--
-- ``generated_at`` is an intentional stand-in for a synthetic
-- MATERIALIZED VIEW's refresh timestamp: using ``MAX(created_at)``
-- keeps the view pure (no trigger, no matview maintenance) while
-- still telling the operator "this snapshot is fresh as of <time>".
-- -----------------------------------------------------------------------------
CREATE VIEW jobs_health_timeseries AS
SELECT
    ats_type,
    COALESCE(status, '(unknown)')             AS status,
    DATE_TRUNC('hour', created_at)            AS bucket_hour,
    COUNT(*)                                  AS row_count,
    MIN(created_at)                           AS earliest_in_bucket,
    MAX(created_at)                           AS latest_in_bucket,
    NOW() - DATE_TRUNC('hour', created_at)    AS bucket_age
FROM jobs
GROUP BY ats_type, status, DATE_TRUNC('hour', created_at)
ORDER BY bucket_hour DESC, ats_type, status;
