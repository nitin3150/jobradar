import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createQAEntry, deleteQAEntry, fetchQABank, updateQAEntry } from '../api/jobs';

export function useQABank() {
  return useQuery({
    queryKey: ['qa-bank'],
    queryFn: fetchQABank,
    staleTime: 60000,
  });
}

export function useUpdateQAEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }) => updateQAEntry(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['qa-bank'] }),
  });
}

export function useCreateQAEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createQAEntry,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['qa-bank'] }),
  });
}

export function useDeleteQAEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteQAEntry,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['qa-bank'] }),
  });
}
