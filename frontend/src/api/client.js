import axios from 'axios';

const API_URL = import.meta.env.VITE_API_URL || '';

const api = axios.create({
  baseURL: `${API_URL}/api`,
  headers: { 'Content-Type': 'application/json' },
});

// Companies
export const fetchCompanies = (params) => api.get('/companies', { params }).then((r) => r.data);
export const fetchCompanyStats = () => api.get('/companies/stats').then((r) => r.data);
export const fetchCompany = (id) => api.get(`/companies/${id}`).then((r) => r.data);
export const updateCompanyStatus = (id, status) =>
  api.patch(`/companies/${id}/status`, { status }).then((r) => r.data);

// Outreach
export const generateOutreach = (data) => api.post('/outreach/generate', data).then((r) => r.data);
export const fetchOutreachMessages = (companyId) =>
  api.get(`/outreach/${companyId}`).then((r) => r.data);

// Pipeline
export const triggerPipeline = () => api.post('/pipeline/run').then((r) => r.data);
export const fetchPipelineStatus = () => api.get('/pipeline/status').then((r) => r.data);
export const triggerDiscovery = () => api.post('/pipeline/discover').then((r) => r.data);
export const fetchSchedule = () => api.get('/pipeline/schedule').then((r) => r.data);
export const updateSchedule = (intervalHours) =>
  api.put('/pipeline/schedule', { interval_hours: intervalHours }).then((r) => r.data);

// Settings (user preferences — server-side singleton, multi-device sync)
export const fetchPreferences = () => api.get('/settings').then((r) => r.data);
export const updatePreferences = (patch) => api.patch('/settings', patch).then((r) => r.data);

export default api;
