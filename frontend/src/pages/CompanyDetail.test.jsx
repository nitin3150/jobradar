import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import CompanyDetail from './CompanyDetail';

// Mock the data hooks so CompanyDetail doesn't hit React Query's
// real network/cache wiring. The "Related Jobs" section is the
// focus of this test surface.
vi.mock('../hooks/useCompanies', () => ({
  useCompany: vi.fn(),
}));

vi.mock('../hooks/useOutreach', () => ({
  useOutreachMessages: vi.fn(() => ({ data: [] })),
}));

vi.mock('../hooks/useJobs', () => ({
  useJobs: vi.fn(),
  // ``Navbar`` (rendered by CompanyDetail) calls ``usePendingCount``;
  // the mock must provide it or the test crashes on first render.
  usePendingCount: vi.fn(() => ({ data: { count: 0 } })),
}));

import { useCompany } from '../hooks/useCompanies';
import { useJobs } from '../hooks/useJobs';

const sampleCompany = {
  id: 'c-1',
  name: 'Replicate',
  website: 'https://replicate.com',
  funding_amount: 5_000_000,
  funding_stage: 'seed',
  source: 'producthunt',
  status: 'interested',
  hiring_intent_score: 78,
  company_summary: 'Fast inference infra for ML models.',
  likely_roles: ['ML Engineer'],
  hiring_signals: ['Hiring backend engineers'],
  founder_name: 'J. Doe',
};

const sampleJobs = [
  {
    id: 'j-1',
    status: 'in_review',
    ats_type: 'ashby',
    title: 'Senior AI Engineer',
    company_name: 'Replicate',
    ai_fit_score: 0.86,
  },
  {
    id: 'j-2',
    status: 'approved',
    ats_type: 'lever',
    title: 'Founding Engineer',
    company_name: 'Replicate',
    ai_fit_score: 0.78,
  },
];

function makeWrapper(initialPath = '/company/c-1') {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  function Wrapper({ children }) {
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/company/:id" element={children} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }
  return { QueryProvider: Wrapper, qc };
}

describe('CompanyDetail — Related Jobs section', () => {
  beforeEach(() => {
    useCompany.mockReset();
    useJobs.mockReset();
  });

  it('renders the Related Jobs section heading', () => {
    useCompany.mockReturnValue({ data: sampleCompany, isLoading: false });
    useJobs.mockReturnValue({ data: { jobs: sampleJobs, total: 2 }, isLoading: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <CompanyDetail />
      </QueryProvider>
    );
    expect(screen.getByText(/Related Jobs/i)).toBeInTheDocument();
  });

  it('renders one row per job returned by the company_id filter', () => {
    useCompany.mockReturnValue({ data: sampleCompany, isLoading: false });
    useJobs.mockReturnValue({ data: { jobs: sampleJobs, total: 2 }, isLoading: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <CompanyDetail />
      </QueryProvider>
    );
    expect(screen.getByText(/Senior AI Engineer/i)).toBeInTheDocument();
    expect(screen.getByText(/Founding Engineer/i)).toBeInTheDocument();
  });

  it('shows a "no jobs" empty state when the filter returns an empty list', () => {
    useCompany.mockReturnValue({ data: sampleCompany, isLoading: false });
    useJobs.mockReturnValue({ data: { jobs: [], total: 0 }, isLoading: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <CompanyDetail />
      </QueryProvider>
    );
    expect(screen.getByText(/No jobs from this company/i)).toBeInTheDocument();
  });

  it('passes the company id to useJobs so the SQL filter is correct', () => {
    useCompany.mockReturnValue({ data: sampleCompany, isLoading: false });
    useJobs.mockReturnValue({ data: { jobs: [], total: 0 }, isLoading: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <CompanyDetail />
      </QueryProvider>
    );
    expect(useJobs).toHaveBeenCalled();
    // The filter object must include the company id so the
    // backend ``?company_id=<uuid>`` query goes out.
    const callArgs = useJobs.mock.calls.at(-1)[0];
    expect(callArgs.company_id).toBe('c-1');
  });

  it('links each Related Jobs row to the JobDetail page', () => {
    useCompany.mockReturnValue({ data: sampleCompany, isLoading: false });
    useJobs.mockReturnValue({ data: { jobs: sampleJobs, total: 2 }, isLoading: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <CompanyDetail />
      </QueryProvider>
    );
    // The first row's title is a Link to /jobs/j-1.
    const link = screen.getByText(/Senior AI Engineer/i).closest('a');
    expect(link).toHaveAttribute('href', '/jobs/j-1');
  });
});
