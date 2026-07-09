import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createApplicationFromJob,
  fetchApplications,
  updateApplicationStatus,
} from '../api/jobs';

export function useApplications(filters = {}) {
  return useQuery({
    queryKey: ['applications', filters],
    queryFn: () => fetchApplications(filters),
    staleTime: 30000,
    refetchInterval: 60000,
  });
}

export function useUpdateApplicationStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status, notes }) => updateApplicationStatus(id, status, notes),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['applications'] });
    },
  });
}

// Manual-apply handoff — called by the JobsReview "Mark as applied"
// button. Invalidates BOTH the jobs cache (so the card moves out of
// the 'approved' tab and into the 'applied' tab) AND the applications
// cache (so the new row appears in ApplicationTracker). The single
// ``['jobs']`` invalidate also covers the Navbar pending-count badge
// because React Query treats ``['jobs', ...]`` as a prefix match.
export function useCreateApplication() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ jobId, notes }) => createApplicationFromJob(jobId, notes),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
      qc.invalidateQueries({ queryKey: ['applications'] });
    },
  });
}
