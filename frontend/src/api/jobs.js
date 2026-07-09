import api from './client';

// Jobs
export const fetchJobs = (params) => api.get('/jobs', { params }).then((r) => r.data);
export const fetchPendingCount = () => api.get('/jobs/pending-count').then((r) => r.data);
export const approveJob = (id) => api.post(`/jobs/${id}/approve`).then((r) => r.data);
export const rejectJob = (id) => api.post(`/jobs/${id}/reject`).then((r) => r.data);
// Applications
export const fetchApplications = (params) =>
  api.get('/applications', { params }).then((r) => r.data);
export const updateApplicationStatus = (id, status, notes) =>
  api.patch(`/applications/${id}/status`, { status, notes }).then((r) => r.data);
// Manual-apply handoff — create an Application row from a job_id and
// atomically flip the linked Job to status='applied'. The JobsReview
// Mark as applied button POSTs here after opening the job URL in a new
// tab; the backend enforces the state-machine guard (only 'approved'
// jobs can transition to 'applied') and returns the new Application.
export const createApplicationFromJob = (jobId, notes) =>
  api.post('/applications', { job_id: jobId, notes }).then((r) => r.data);

// Q&A Bank
export const fetchQABank = () => api.get('/qa-bank').then((r) => r.data);
export const createQAEntry = (data) => api.post('/qa-bank', data).then((r) => r.data);
export const updateQAEntry = (id, data) =>
  api.patch(`/qa-bank/${id}`, data).then((r) => r.data);
export const deleteQAEntry = (id) => api.delete(`/qa-bank/${id}`).then((r) => r.data);
