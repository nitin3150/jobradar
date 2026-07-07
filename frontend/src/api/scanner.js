import api from './client';

// Five-domain scanner API. Each tab in the navbar calls one of these.
export const runScanner = async (domain, { delta_hours = 24, limit = 50, sources, languages, boards } = {}) => {
  const params = { delta_hours, limit };
  if (sources?.length) params.sources = sources.join(',');
  if (languages?.length) params.languages = languages.join(',');
  if (boards?.length) params.boards = boards.join(',');
  const response = await api.post(`/scan/${domain}`, null, { params });
  return response.data;
};

export const runFunding = (opts) => runScanner('funding', opts);
export const runNgos = (opts) => runScanner('ngos', opts);
export const runRemote = (opts) => runScanner('remote', opts);
export const runBoards = (opts) => runScanner('boards', opts);
export const runOss = (opts) => runScanner('oss', opts);
