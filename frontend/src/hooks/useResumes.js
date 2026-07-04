import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteResume,
  fetchResumes,
  updateResume,
  uploadResume,
} from '../api/resumes';

export function useResumes() {
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ['resumes'],
    queryFn: fetchResumes,
    staleTime: 30_000,
  });

  const uploadMutation = useMutation({
    mutationFn: ({ file, tags, isDefault, onProgress }) =>
      uploadResume(file, { tags, isDefault, onProgress }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['resumes'] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteResume,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['resumes'] });
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, patch }) => updateResume(id, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['resumes'] });
    },
  });

  return {
    resumes: data ?? [],
    isLoading,
    error,
    upload: uploadMutation.mutate,
    uploadAsync: uploadMutation.mutateAsync,
    isUploading: uploadMutation.isPending,
    uploadError: uploadMutation.error,
    uploadProgress: uploadMutation.variables?.onProgress ?? null,
    remove: deleteMutation.mutate,
    isRemoving: deleteMutation.isPending,
    update: updateMutation.mutate,
    updateAsync: updateMutation.mutateAsync,
    isUpdating: updateMutation.isPending,
  };
}

export function formatBytes(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
