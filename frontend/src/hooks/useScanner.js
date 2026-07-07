import { useQuery } from '@tanstack/react-query';
import { runScanner } from '../api/scanner';

// Default delta_hours tuned per domain. The user can override via filters
// later; these mirror the backend's own defaults so the first paint shows
// fresh content rather than empty-state flashes.
const DEFAULT_DELTA_HOURS = {
  funding: 168, // weekly; sparse updates
  ngos: 72,
  remote: 24,
  boards: 1, // hourly scan
  oss: 168,
};

export function useScannerOpportunities(category, { delta_hours, limit = 50, sources, languages } = {}) {
  const params = {
    delta_hours: delta_hours ?? DEFAULT_DELTA_HOURS[category] ?? 24,
    limit,
  };
  if (sources?.length) params.sources = sources.join(',');
  if (languages?.length) params.languages = languages.join(',');

  return useQuery({
    queryKey: ['scanner', category, params],
    queryFn: () => runScanner(category, params),
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: 1,
    enabled: !!category,
  });
}
