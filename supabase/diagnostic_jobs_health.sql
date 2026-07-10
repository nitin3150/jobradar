-- =============================================================================
-- JobRadar — jobs-table health diagnostic (saved query for Supabase Studio)
-- =============================================================================
--
-- Paste-and-run in: Supabase Dashboard → SQL Editor → New Query → paste here.
--
-- Reads-only — ``SELECT`` statements only. Safe to run on production. The
-- views these queries depend on (``jobs_health_summary`` and
-- ``jobs_health_timeseries``) were created by migration ``20260710000000``.
--
-- When to run this
-- ----------------
-- Run when the React JobBoard page count looks suspicious. Specifically:
--
--   * the page says "Showing N of M" and N is dramatically different from
--     what you expect ("I had 300 jobs in the queue last week, now it shows
--     12"),
--   * every job has the same board badge (e.g. every card says "Ashby" but
--     yesterday they were a mix of Lever / Greenhouse / Ashby),
--   * the dates filter returns empty even though every card has a date
--     ("I set the filter to last week, zero results").
--
-- Reading order
-- -------------
-- Read the queries **top → bottom**. Each one answers a single question;
-- if a query's result is unexpected, the queries below it localize the
-- issue. ① tells you WHAT is wrong. ② tells you WHEN it happened. ③
-- confirms the row-count at a glance. ④ isolates a single row for
-- smell-testing (did a particular board / company get wiped?).
--
-- Designed to be "first query" the operator runs on any DB-shape
-- suspicion — the four queries together total under 100 ms on the
-- operator's 12-row instance.
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- ① WHAT — at-a-glance table shape by (board, status).
--
-- Expected on a healthy DB:
--   * Total row count ≈ 100–500 rows (operator-tuned).
--   * Several ``ats_type`` values, mixed with several ``status`` values.
--   * ``oldest_row_created_at`` is at least several days old on a steady-state
--     that's been running for weeks.
--
-- Red flags:
--   * Single ``ats_type`` only = DB is being scraped by a single older
--     worker / scrape path wrote everything.
--   * Single ``status`` only = scoring pipeline is broken (all rows stuck
--     in ``in_review`` or all flipped to terminal without review).
--   * ``oldest_row_created_at`` is within the last few hours = a destructive
--     migration or an env swap just happened.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    ats_type,
    status,
    COUNT(*)                                  AS row_count,
    MIN(created_at)                           AS oldest_row_created_at,
    MAX(created_at)                           AS newest_row_created_at,
    MAX(updated_at)                           AS last_touched_at
FROM jobs
GROUP BY ats_type, status
ORDER BY ats_type, status;


-- ─────────────────────────────────────────────────────────────────────────────
-- ② WHEN — hour-bucketed insert volume (env-swap / destructive-migration detector).
--
-- Expected on a healthy DB (operator's GHA scans every 1h, daily dormant
-- scan, weekly cleanup):
--   * Buckets spaced ~1h apart going back 24h.
--   * Sharp drop after the boundary to hourly vs 24h delta_hours.
--   * Some zero-bucket hours on quiet nights (mostly weekday evenings UTC).
--
-- Red flags (read this query carefully when the DB feels wrong):
--   * ONE bucket contains everything, and ``bucket_hour`` is within the
--     last hour → env swap. The Supabase URL secret in GHA + Render is
--     pointing at a different (empty) project; restore the original URL.
--   * ``bucket_hour`` has a clean G then nothing for an interval then
--     resumes N → destructive migration. Recovery requires either
--     triggering a dormant-tier re-scan or restoring from a backup.
--   * All buckets within 1h of the same value → scrape-frequency broken.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    DATE_TRUNC('hour', created_at)            AS bucket_hour,
    COUNT(*)                                  AS row_count
FROM jobs
GROUP BY DATE_TRUNC('hour', created_at)
ORDER BY 1 DESC
LIMIT 48;


-- ─────────────────────────────────────────────────────────────────────────────
-- ③ TOP-LINE — single-row summary of the whole table.
--
-- The "yes/no" check before diving into ① and ②. If ``total_jobs`` is
-- dramatically lower than expected (e.g. < 50 when the operator expects
-- 300+), start with ① and work down. ``gap_since_last_row`` should be
-- reasonable — a fresh GHA scan typically lands every 1h, so a gap > 4h
-- means the GHA workflow itself is misfiring.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    COUNT(*)                                  AS total_jobs,
    NOW() - MAX(created_at)                   AS gap_since_last_row,
    NOW() - MIN(created_at)                   AS table_age
FROM jobs;


-- ─────────────────────────────────────────────────────────────────────────────
-- ④ SPOT-CHECK — oldest row + newest row, side-by-side.
--
-- If ① or ② surfaces a clear break (e.g. "every row is 1h old but I
-- expect the oldest to be 3 weeks old"), this is the row to look at
-- first. Copy-paste an ``id`` into the React JobBoard URL
-- (``/jobs/<uuid>``) to see the full record.
-- ─────────────────────────────────────────────────────────────────────────────
(SELECT 'OLDEST' AS tag, id, ats_type, status, company_name, title,
        created_at, updated_at
 FROM jobs
 ORDER BY created_at ASC
 LIMIT 1)
UNION ALL
(SELECT 'NEWEST' AS tag, id, ats_type, status, company_name, title,
        created_at, updated_at
 FROM jobs
 ORDER BY created_at DESC
 LIMIT 1);
