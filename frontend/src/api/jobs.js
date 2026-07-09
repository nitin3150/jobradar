import api from './client';

// Jobs
// -----
// Wire shape that the React JobBoard page consumes. The server's
// ``GET /api/jobs`` accepts the filter set below and returns an
// envelope shaped ``{ jobs, total, page, page_size }``. ``jobs`` is
// an array of cards with the field set the JobBoard page reads
// directly — see :class:`JobBoard` for the field-by-field contract.
//
// ``fetchJobs`` keeps the OLD call shape working too: callers that
// pass ``{}`` get the default ``page_size=50, page=1`` request. New
// callers pass the full filter object — arrays serialise to
// ``status=a&status=b``, dates/strings serialise to their natural
// ``?key=value`` form.
//
// ``patchJobStatus`` replaces the per-status POST endpoints
// (approve / reject). The backend will write a ``job_status_history``
// row in the same tx; this hook returns the updated Job and lets
// the React-Query invalidate cascade pick the rest up.
//
// ``requestResearch`` is the sync Interview Prep call. v1 returns
// the rendered Markdown body, the model used, and the
// ``requested_at`` / ``generated_at`` timestamps so the modal can
// show provenance.

export const fetchJobs = (params) => api.get('/jobs', { params }).then((r) => r.data);
// Single-job lookup. Drives the React ``JobDetail`` page. The
// server returns the same ``Job`` Pydantic model the list endpoint
// emits per-row, so the React JobCard / JobDetail consumers share
// the same field set without a separate ``JobDetail`` schema. 404
// surfaces as an axios error with the server's ``detail`` field.
export const fetchJob = (id) => api.get(`/jobs/${id}`).then((r) => r.data);
export const fetchPendingCount = () => api.get('/jobs/pending-count').then((r) => r.data);

// Generic status PATCH. Replaces the per-status POST endpoints in
// ``useJobs.js`` so the UI sends the target status directly. Body
// shape: ``{ status: 'in_review'|'approved'|'rejected'|'applied'|'flagged',
// source?: 'user'|'auto_apply', note?: string }``.
export const patchJobStatus = (id, body) =>
  api.patch(`/jobs/${id}/status`, body).then((r) => r.data);

// Sync Interview Prep call. Returns either a ``ready`` envelope with
// Markdown ``content`` + ``model_used`` and timestamps, or a
// ``failed`` envelope with an ``error`` field. The server persists the
// result to ``research_reports`` so a future re-render is a single
// ``GET /api/jobs/{id}/research`` away.
export const requestResearch = (jobId) =>
  api.post(`/jobs/${jobId}/research`).then((r) => r.data);

// Get the most recent research report for a job. Lets the modal
// re-open a previously-generated brief without a fresh LLM call.
export const fetchLatestResearch = (jobId) =>
  api.get(`/jobs/${jobId}/research`).then((r) => r.data);

// Legacy POST endpoints — kept for the existing ApprovalModal that
// the dashboard widget still uses; the JobCard now prefers
// ``patchJobStatus``. Bus-compat shims so an in-flight review widget
// doesn't crash if it lands before the route-rewrite PR.
export const approveJob = (id) => api.post(`/jobs/${id}/approve`).then((r) => r.data);
export const rejectJob = (id) => api.post(`/jobs/${id}/reject`).then((r) => r.data);

// Applications
// ------------
export const fetchApplications = (params) =>
  api.get('/applications', { params }).then((r) => r.data);
export const updateApplicationStatus = (id, status, notes) =>
  api.patch(`/applications/${id}/status`, { status, notes }).then((r) => r.data);

// Manual-apply handoff — create an Application row from a job_id and
// atomically flip the linked Job to status='applied'. The JobBoard
// "Mark as applied" path POSTs here after opening the job URL in a
// new tab; the backend enforces the state-machine guard (only
// 'approved' jobs can transition to 'applied') and returns the new
// Application.
export const createApplicationFromJob = (jobId, notes) =>
  api.post('/applications', { job_id: jobId, notes }).then((r) => r.data);

// Q&A Bank
// --------
export const fetchQABank = () => api.get('/qa-bank').then((r) => r.data);
export const createQAEntry = (data) => api.post('/qa-bank', data).then((r) => r.data);
export const updateQAEntry = (id, data) =>
  api.patch(`/qa-bank/${id}`, data).then((r) => r.data);
export const deleteQAEntry = (id) => api.delete(`/qa-bank/${id}`).then((r) => r.data);
