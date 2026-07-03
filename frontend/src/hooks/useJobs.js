import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { approveJob, fetchJobs, fetchPendingCount, rejectJob } from '../api/jobs';

export function useJobs(filters = {}) {
  return useQuery({
    queryKey: ['jobs', filters],
    queryFn: () => fetchJobs(filters),
    staleTime: 30000,
    refetchInterval: 60000,
  });
}

export function usePendingCount() {
  return useQuery({
    queryKey: ['jobs', 'pending-count'],
    queryFn: fetchPendingCount,
    refetchInterval: 30000,
  });
}

export function useApproveJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: approveJob,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}

export function useRejectJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: rejectJob,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}
