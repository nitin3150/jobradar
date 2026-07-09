-- =============================================================================
-- JobRadar — initial schema (Supabase CLI mirror)
-- =============================================================================
--
-- This file is a flat, byte-equivalent translation of the Alembic initial
-- migration in ``backend/db/migrations/versions/0001_initial_schema.py``.
-- Supabase CLI applies migrations in timestamp-sorted order; this one runs
-- first.
--
-- Design decisions are documented in
-- docs/02-version2-add-postgres.md and docs/project-overview.md §7.3. The
-- schema here matches ``backend/db/models.py`` exactly so ``supabase db
-- diff`` is empty after a successful apply.
--
-- Why no RLS policies
-- --------------------
-- JobRadar is a single-user self-hosted app; the FastAPI proxy is the sole
-- owner of the data. RLS would force every future route rewrite to inject
-- ``user_id = auth.uid()`` predicates, and the schema doesn't reserve a
-- ``user_id`` column. Leave RLS off until multi-user becomes a goal.
--
-- Sync contract
-- -------------
-- Any change here MUST be mirrored to
-- ``backend/db/migrations/versions/0001_initial_schema.py`` (and vice
-- versa).
--
-- Apply:    supabase db push    (from the repo root)
-- Rollback: supabase db reset   (drops + re-applies)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. pgcrypto — provides ``gen_random_uuid()``. Supabase enables it by
--    default; ``CREATE EXTENSION IF NOT EXISTS`` is a no-op there but
--    saves the operator from a confusing error on a vanilla PG 16 install.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- -----------------------------------------------------------------------------
-- 2. Enum types. Keep this list in sync with the ``postgresql.ENUM``
--    instances in :mod:`backend.db.models` and the Literal[...] Pydantic
--    enums in backend/routes/* — expansions go on all three sides together.
-- -----------------------------------------------------------------------------
CREATE TYPE company_category AS ENUM (
    'boards', 'funding', 'ngos', 'oss', 'remote'
);
CREATE TYPE company_status AS ENUM (
    'saved', 'interested', 'dismissed', 'outreach_sent', 'engaged'
);
CREATE TYPE job_status AS ENUM (
    'in_review', 'approved', 'rejected', 'applied', 'flagged'
);
CREATE TYPE application_status AS ENUM (
    'submitted', 'interview', 'rejected', 'offer', 'ghosted'
);
CREATE TYPE qa_answer_type AS ENUM (
    'short_text', 'long_text'
);
CREATE TYPE outreach_type AS ENUM (
    'email', 'twitter_dm', 'linkedin'
);
CREATE TYPE scanner_kind AS ENUM (
    'funding', 'remote', 'ngos', 'oss', 'boards'
);
CREATE TYPE ats_board AS ENUM (
    'ashby', 'lever', 'greenhouse', 'remotive', 'remoteok', 'hackernews'
);
CREATE TYPE ats_org_status AS ENUM (
    'active', 'missing', 'benched'
);
CREATE TYPE pipeline_state AS ENUM (
    'idle', 'running', 'error'
);


-- -----------------------------------------------------------------------------
-- 3. Tables, in FK dependency order: leaves first, FK roots last. The
--    ``companies`` table is a root (5 of 16 tables FK to it), so it lands
--    early. The singletons (``preferences``, ``pipeline_status``) land
--    last so they don't hold up the rest of the install.
-- -----------------------------------------------------------------------------

-- 3.1 companies — user-facing CRM spine (matches routes/companies.Company 1-for-1)
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    organization TEXT NOT NULL,
    url TEXT,
    category company_category NOT NULL,
    score FLOAT NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    source TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}'::text[],
    description TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    location TEXT,
    primary_language TEXT,
    difficulty TEXT,
    stars INTEGER,
    company_summary TEXT,
    hiring_signals TEXT[] NOT NULL DEFAULT '{}'::text[],
    status company_status NOT NULL DEFAULT 'saved',
    external_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_companies_feed
    ON companies (category, status, published_at DESC);
CREATE INDEX idx_companies_external_id
    ON companies (external_id);


-- 3.2 5 raw_scrapes_* landing tables — same DDL, separate heap per scanner
--     so each loader doesn't contend for locks on a unified table.
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'raw_scrapes_funding',
        'raw_scrapes_remote',
        'raw_scrapes_ngos',
        'raw_scrapes_oss',
        'raw_scrapes_boards'
    ]
    LOOP
        EXECUTE format($f$
            CREATE TABLE %I (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                external_id TEXT,
                source TEXT NOT NULL,
                raw_payload JSONB NOT NULL,
                score FLOAT,
                company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                promoted_at TIMESTAMPTZ
            )
        $f$, tbl);

        EXECUTE format($f$
            CREATE INDEX %I ON %I (scraped_at DESC)
        $f$,
            'idx_raw_' || substring(tbl FROM 'raw_scrapes_(.*)') || '_scraped_at',
            tbl
        );
    END LOOP;
END
$$;

-- Per-table external_id indexes for the two landing tables whose dedupe
-- lookup is on the hot path today.
CREATE INDEX idx_raw_funding_external ON raw_scrapes_funding (external_id);
CREATE INDEX idx_raw_boards_external  ON raw_scrapes_boards (external_id);


-- 3.3 scanner_runs — audit trail per LangGraph / boards-runner invocation
CREATE TABLE scanner_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scanner scanner_kind NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    state pipeline_state NOT NULL DEFAULT 'running',
    items_found INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
CREATE INDEX idx_scanner_runs_scanner_started
    ON scanner_runs (scanner, started_at DESC);


-- 3.4 jobs — review queue (matches routes/jobs.Job 1-for-1)
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
    company_name TEXT NOT NULL,
    status job_status NOT NULL,
    ats_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    ai_fit_score FLOAT CHECK (ai_fit_score IS NULL OR (ai_fit_score >= 0.0 AND ai_fit_score <= 1.0)),
    ai_fit_reasoning TEXT,
    review_deadline TIMESTAMPTZ,
    external_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Partial index — keeps the in-review queue sub-millisecond even when ``jobs``
-- grows into the hundreds-of-thousands range.
CREATE INDEX idx_jobs_in_review_deadline
    ON jobs (review_deadline ASC)
    WHERE status = 'in_review';
CREATE INDEX idx_jobs_status_created
    ON jobs (status, created_at DESC);
CREATE INDEX idx_jobs_external
    ON jobs (external_id);


-- 3.5 applications — post-application tracker (matches routes/applications.Application 1-for-1)
CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    job_title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL,
    status application_status NOT NULL,
    last_email_at TIMESTAMPTZ,
    submission_screenshot_path TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_applications_status_submitted
    ON applications (status, submitted_at DESC);


-- 3.6 qa_bank_entries — Q&A bank (matches routes/qa_bank.QAEntry 1-for-1)
CREATE TABLE qa_bank_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_pattern TEXT NOT NULL UNIQUE,
    canonical_question TEXT NOT NULL,
    answer TEXT,
    answer_type qa_answer_type NOT NULL,
    times_used INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_qa_bank_times_used
    ON qa_bank_entries (times_used DESC);


-- 3.7 resumes — metadata only (bytes live in Supabase Storage)
CREATE TABLE resumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}'::text[],
    is_default BOOLEAN NOT NULL DEFAULT false,
    storage_path TEXT NOT NULL
);
-- Single-default invariant: at most one row may have is_default = true.
-- The partial unique index enforces this in the engine — the application
-- layer no longer needs to demote siblings on PATCH.
CREATE UNIQUE INDEX uq_resumes_single_default
    ON resumes (is_default)
    WHERE is_default = true;


-- 3.8 outreach_messages — matches routes/outreach.OutreachMessage 1-for-1
CREATE TABLE outreach_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
    type outreach_type NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    resume_id TEXT,
    resume_name TEXT,
    qa_snippet_id TEXT,
    qa_snippet TEXT
);
CREATE INDEX idx_outreach_company_created
    ON outreach_messages (company_id, created_at DESC);


-- 3.9 preferences — singleton (matches routes/settings.Preferences 1-for-1)
CREATE TABLE preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1) DEFAULT 1,
    target_roles TEXT[] NOT NULL DEFAULT '{}'::text[],
    review_window_hours FLOAT NOT NULL
        CHECK (review_window_hours >= 0.5 AND review_window_hours <= 48.0)
        DEFAULT 2.0,
    job_fit_threshold FLOAT NOT NULL
        CHECK (job_fit_threshold >= 0.0 AND job_fit_threshold <= 1.0)
        DEFAULT 0.6,
    send_followup_emails BOOLEAN NOT NULL DEFAULT true,
    -- Optional seniority band — drives utils.filters.is_relevant_role.
    -- Free-text rather than Postgres ENUM so a tier added on the
    -- Python side doesn't need an ALTER TYPE; Pydantic validates the
    -- wire form so only known values reach the DB.
    min_seniority TEXT NULL,
    max_seniority TEXT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- 3.10 pipeline_status — singleton
CREATE TABLE pipeline_status (
    id INTEGER PRIMARY KEY CHECK (id = 1) DEFAULT 1,
    state pipeline_state NOT NULL DEFAULT 'idle',
    last_run_at TIMESTAMPTZ,
    last_run_duration_seconds FLOAT,
    last_run_counts JSONB,
    recent_error TEXT,
    interval_hours INTEGER NOT NULL
        CHECK (interval_hours IN (1, 2, 4, 6, 12, 24))
        DEFAULT 1,
    schedule_updated_at TIMESTAMPTZ
);


-- 3.11 board_seen_jobs — replaces backend/data/seen.json
CREATE TABLE board_seen_jobs (
    job_id_hash TEXT PRIMARY KEY,
    board ats_board NOT NULL,
    company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_board_seen_board_last_seen
    ON board_seen_jobs (board, last_seen_at DESC);


-- 3.12 ats_discovered_orgs — replaces backend/data/<board>_missing_orgs.json
CREATE TABLE ats_discovered_orgs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    board ats_board NOT NULL,
    slug TEXT NOT NULL,
    company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
    status ats_org_status NOT NULL DEFAULT 'active',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_checked_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_ats_orgs_board_slug UNIQUE (board, slug)
);
CREATE INDEX idx_ats_orgs_status
    ON ats_discovered_orgs (status, consecutive_failures);
