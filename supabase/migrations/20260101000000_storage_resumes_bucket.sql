-- =============================================================================
-- JobRadar — Storage bucket: resumes
-- =============================================================================
--
-- Creates the private ``resumes`` bucket that backend/storage/supabase.py
-- uploads to. The route layer persists the resulting storage path into
-- ``resumes.storage_path`` so the read side round-trips it back via
-- ``GET /api/resumes/{id}/download``.
--
-- Why private
-- -----------
-- The backend authenticates as the service role and serves the bytes
-- through the FastAPI proxy. There is no need to expose the bucket over
-- a public URL — keeping it private also means future RLS deploys have
-- a clean starting line.
--
-- Apply: supabase db push (or run this file in the SQL editor)
-- =============================================================================

INSERT INTO storage.buckets (id, name, public)
VALUES ('resumes', 'resumes', false)
ON CONFLICT (id) DO NOTHING;
