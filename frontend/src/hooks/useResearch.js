import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId) => requestResearch(jobId),
    // Invalidate the cached ``['research', jobId]`` query so the
    // JobDetail page + InterviewPrepModal both pick up the new
    // brief immediately after a Regenerate click. Without this the
    // mutation succeeds server-side but the displayed content stays
    // stale for up to the 60s ``useLatestResearch`` staleTime.
    onSuccess: (_data, jobId) => {
      qc.invalidateQueries({ queryKey: [...RESEARCH_KEY, jobId] });
    },
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
