import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import JobDetail from './JobDetail';

// Mock the hooks so JobDetail doesn't hit React Query's real
// network/cache wiring. We inject deterministic data + spies
// on the query/mutation functions to verify the page wiring.
vi.mock('../hooks/useJobs', () => ({
  useJob: vi.fn(),
  useJobStatus: vi.fn(),
}));

vi.mock('../hooks/useResearch', () => ({
  useLatestResearch: vi.fn(),
  useResearchMutation: vi.fn(),
}));

import { useJob, useJobStatus } from '../hooks/useJobs';
import { useLatestResearch, useResearchMutation } from '../hooks/useResearch';

const sampleJob = {
  id: '11111111-1111-1111-1111-111111111111',
  status: 'in_review',
  ats_type: 'ashby',
  title: 'Senior AI Engineer',
  company_name: 'Replicate',
  url: 'https://replicate.com/careers',
  ai_fit_score: 0.86,
  ai_fit_reasoning: 'Strong match — LLM inference + Python + open-source fluency.',
  review_deadline: '2026-07-10T18:00:00Z',
  posted_at: '2026-07-09T10:00:00Z',
  source_updated_at: '2026-07-09T10:00:00Z',
  created_at: '2026-07-09T10:05:00Z',
  updated_at: '2026-07-09T10:05:00Z',
};

function makeWrapper(initialPath = '/jobs/abc') {
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
            <Route path="/jobs" element={<div>Job Board List</div>} />
            <Route path="/jobs/:id" element={children} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }
  return { QueryProvider: Wrapper, qc };
}

describe('JobDetail — page wiring + research section', () => {
  beforeEach(() => {
    useJob.mockReset();
    useJobStatus.mockReset();
    useLatestResearch.mockReset();
    useResearchMutation.mockReset();
  });

  it('shows a loading skeleton while useJob is loading', () => {
    useJob.mockReturnValue({ data: undefined, isLoading: true, isError: false, error: null });
    useJobStatus.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useLatestResearch.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    useResearchMutation.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    // The animate-pulse skeleton renders animated divs; we just
    // check that the page didn't render any of the loaded-state
    // copy yet.
    expect(screen.queryByText(/Senior AI Engineer/i)).not.toBeInTheDocument();
  });

  it('renders the job title, company, ats_type, and score when loaded', () => {
    useJob.mockReturnValue({ data: sampleJob, isLoading: false, isError: false, error: null });
    useJobStatus.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useLatestResearch.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    useResearchMutation.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    expect(screen.getByText(/Senior AI Engineer/i)).toBeInTheDocument();
    expect(screen.getByText(/Replicate/)).toBeInTheDocument();
    expect(screen.getByText(/ashby/)).toBeInTheDocument();
    // Score renders 86% (rounded from 0.86).
    expect(screen.getByText(/86% fit/)).toBeInTheDocument();
  });

  it('shows the AI reasoning block when present', () => {
    useJob.mockReturnValue({ data: sampleJob, isLoading: false, isError: false, error: null });
    useJobStatus.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useLatestResearch.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    useResearchMutation.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    expect(screen.getByText(/AI Reasoning/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Strong match — LLM inference \+ Python/i)
    ).toBeInTheDocument();
  });

  it('shows a 404 message when the server returns 404', () => {
    useJob.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { response: { status: 404 } },
    });
    useJobStatus.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useLatestResearch.mockReturnValue({ data: undefined, isLoading: false, isError: false });
    useResearchMutation.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    expect(screen.getByText(/not found/i)).toBeInTheDocument();
  });

  it('shows a Generate button when no cached research exists', async () => {
    useJob.mockReturnValue({ data: sampleJob, isLoading: false, isError: false, error: null });
    useJobStatus.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useLatestResearch.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    const mutate = vi.fn();
    useResearchMutation.mockReturnValue({ mutate, isPending: false, isError: false });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    const btn = await screen.findByRole('button', { name: /Generate Research Brief/i });
    await user.click(btn);
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledWith(sampleJob.id);
  });

  it('renders the cached research content when a ready report exists', async () => {
    useJob.mockReturnValue({ data: sampleJob, isLoading: false, isError: false, error: null });
    useJobStatus.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useLatestResearch.mockReturnValue({
      data: {
        id: 'r1',
        job_id: sampleJob.id,
        status: 'ready',
        content: '## Company Snapshot\nFast inference infra.',
        model_used: 'meta/llama-3.1-70b-instruct',
        error: null,
        requested_at: '2026-07-09T10:00:00Z',
        generated_at: '2026-07-09T10:00:30Z',
      },
      isLoading: false,
      isError: false,
    });
    useResearchMutation.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    await waitFor(() => {
      expect(screen.getByText(/Company Snapshot/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Fast inference infra\./)).toBeInTheDocument();
  });

  it('fires useJobStatus.mutate when the status dropdown changes', async () => {
    useJob.mockReturnValue({ data: sampleJob, isLoading: false, isError: false, error: null });
    const mutate = vi.fn();
    useJobStatus.mockReturnValue({ mutate, isPending: false });
    useLatestResearch.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    useResearchMutation.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: false });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobDetail />
      </QueryProvider>
    );
    const select = screen.getByRole('combobox', { name: /Status/i });
    await user.selectOptions(select, 'approved');
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledWith({
      id: sampleJob.id,
      status: 'approved',
      source: 'user',
    });
  });
});
