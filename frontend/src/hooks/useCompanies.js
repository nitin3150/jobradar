import { useQuery } from '@tanstack/react-query';
import { fetchCompanies, fetchCompany, fetchCompanyStats } from '../api/client';

export function useCompanies(filters = {}) {
  return useQuery({
    queryKey: ['companies', filters],
    queryFn: () => fetchCompanies(filters),
    staleTime: 30000,
    keepPreviousData: true,
  });
}

export function useCompany(id) {
  return useQuery({
    queryKey: ['company', id],
    queryFn: () => fetchCompany(id),
    enabled: !!id,
  });
}

export function useCompanyStats() {
  return useQuery({
    queryKey: ['companyStats'],
    queryFn: fetchCompanyStats,
    staleTime: 30000,
  });
}
