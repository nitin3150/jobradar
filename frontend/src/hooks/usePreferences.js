import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchPreferences, updatePreferences } from '../api/client';

// Mirror backend/app/models/preferences.py DEFAULT_* — used as a sync fallback
// before the first GET resolves (avoids undefined-iteration rendering flash).
export const DEFAULT_PREFERENCES = {
  target_roles: [
    'AI Engineer',
    'Machine Learning Engineer',
    'LLM Engineer',
    'Software Engineer',
  ],
  review_window_hours: 2,
  job_fit_threshold: 0.6,
  send_followup_emails: true,
};

export function usePreferences() {
  const queryClient = useQueryClient();

  const { data: prefs, isLoading, error } = useQuery({
    queryKey: ['preferences'],
    queryFn: fetchPreferences,
    staleTime: 30_000,
  });

  const mutation = useMutation({
    mutationFn: updatePreferences,
    onSuccess: (next) => {
      // Optimistic reconcilation: trust the server's response so the cache
      // reflects server-side normalization (trim/lower-case/dedupe tags).
      queryClient.setQueryData(['preferences'], next);
      queryClient.invalidateQueries({ queryKey: ['preferences'] });
    },
  });

  return {
    prefs: prefs ?? DEFAULT_PREFERENCES,
    isLoading,
    error,
    save: mutation.mutate,
    saveAsync: mutation.mutateAsync,
    isSaving: mutation.isPending,
    saveError: mutation.error,
  };
}
