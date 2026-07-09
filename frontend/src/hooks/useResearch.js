import { useMutation, useQuery } from '@tanstack/react-query';
import { fetchLatestResearch, requestResearch } from '../api/jobs';

const RESEARCH_KEY = ['research'];

/**
 * Sync Interview Prep hook. The user picked the "block until ready"
 * UX, so this returns the rendered Markdown plus provenance (which
 * model, when generated) the moment the LLM call returns.
 *
 * Two related actions on the same job:
 *   - ``request(jobId)`` — POST /api/jobs/{id}/research, returns the
 *     rendered Markdown body. Heavy (15-60s).
 *   - ``fetchLatest(jobId)`` — GET /api/jobs/{id}/research, returns
 *     the cached research_reports row if one exists; this is what
 *     the modal uses to re-open the LAST report without paying for a
 *     fresh LLM call.
 */
export function useResearchMutation() {
  return useMutation({
    mutationFn: (jobId) => requestResearch(jobId),
    // No query invalidation on success — the research_reports row
    // lives in its own cache namespace (``['research', jobId]``) so
    // the job-list cache stays quiet.
  });
}

export function useLatestResearch(jobId) {
  return useQuery({
    queryKey: [...RESEARCH_KEY, jobId],
    queryFn: () => fetchLatestResearch(jobId),
    // The endpoint returns ``null`` when no report exists yet. The
    // query treats ``null`` as "no result" so the modal can drive
    // off ``data?.content`` without a custom guard.
    retry: false,
    staleTime: 60_000,
  });
}
