import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchPreferences, updatePreferences } from '../api/client';

// Mirror backend/app/models/preferences.py DEFAULT_* — used as a sync fallback
// before the first GET resolves (avoids undefined-iteration rendering flash).
// ``min_seniority`` / ``max_seniority`` are null by default so the band
// filter is a no-op until the operator opts in via the PreferencesModal.
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
  min_seniority: null,
  max_seniority: null,
};

// Single source of truth for the seniority tiers the dropdowns render
// and the modal maps between display label and backend wire value. The
// keys (left side) round-trip verbatim to the Pydantic Literal in
// backend/routes/settings.py — drift between the two surfaces is a
// 422 at the API boundary, intentional: it's how we detect a stale UI.
export const SENIORITY_TIERS = [
  { value: '', label: 'Any seniority' },
  { value: 'intern', label: 'Intern' },
  { value: 'junior', label: 'Junior' },
  { value: 'mid', label: 'Mid-level' },
  { value: 'senior', label: 'Senior' },
  { value: 'staff', label: 'Staff' },
  { value: 'principal', label: 'Principal' },
  { value: 'lead', label: 'Lead' },
  { value: 'manager', label: 'Manager' },
  { value: 'director', label: 'Director' },
  { value: 'vp', label: 'VP / Chief' },
];

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
