import api from './client';

// Resumes — list / upload (multipart) / patch tags-or-default / delete / download URL.

export const fetchResumes = () => api.get('/resumes').then((r) => r.data.resumes);

export const uploadResume = (file, opts = {}) => {
  const fd = new FormData();
  fd.append('file', file);
  if (typeof opts.tags === 'string') {
    fd.append('tags', opts.tags);
  }
  if (opts.isDefault) {
    fd.append('is_default', 'true');
  }
  // The shared axios instance in client.js sets a default
  // `Content-Type: application/json` header. When you pass FormData, axios v1
  // uses any explicitly-provided Content-Type verbatim and skips injecting the
  // multipart boundary — so the body reaches FastAPI as application/json and
  // trips a 422 from the multipart parser before our route runs.
  // Passing `Content-Type: undefined` disables the instance default for this
  // call, which lets axios's FormData branch emit
  //   Content-Type: multipart/form-data; boundary=…
  // correctly.
  return api
    .post('/resumes', fd, {
      headers: { 'Content-Type': undefined },
      onUploadProgress: opts.onProgress,
    })
    .then((r) => r.data);
};

export const updateResume = (id, patch) =>
  api.patch(`/resumes/${id}`, patch).then((r) => r.data);

export const deleteResume = (id) =>
  api.delete(`/resumes/${id}`).then(() => undefined);

export const buildDownloadUrl = (id) => `/api/resumes/${id}/download`;
