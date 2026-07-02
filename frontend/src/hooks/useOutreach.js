import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchOutreachMessages, generateOutreach } from '../api/client';

export function useGenerateOutreach() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: generateOutreach,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['outreach', data.company_id] });
      queryClient.invalidateQueries({ queryKey: ['company', data.company_id] });
    },
  });
}

export function useOutreachMessages(companyId) {
  return useQuery({
    queryKey: ['outreach', companyId],
    queryFn: () => fetchOutreachMessages(companyId),
    enabled: !!companyId,
  });
}
