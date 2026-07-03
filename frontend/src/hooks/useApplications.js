import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApplications, updateApplicationStatus } from '../api/jobs';

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
