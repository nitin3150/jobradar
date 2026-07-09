import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import JobsReview from './JobsReview';

// Mock the jobs + applications hooks so JobsReview doesn't hit React
// Query's real network/cache wiring. We just inject deterministic
// data + spies on the query function to verify pagination + filter
// behavior.
vi.mock('../hooks/useJobs', () => ({
  useJobs: vi.fn(),
  usePendingCount: vi.fn(() => ({ data: { count: 0 } })),
  useApproveJob: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useRejectJob: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));

vi.mock('../hooks/useApplications', () => ({
  useCreateApplication: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));

import { useJobs } from '../hooks/useJobs';

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  function Wrapper({ children }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return { QueryProvider: Wrapper, qc };
}

// Helper: build a stubbed useJobs that returns the supplied data
// synchronously. Records every params object it was called with so
// tests can assert on the filter/page wiring.
function stubUseJobs(data) {
  const callLog = [];
  useJobs.mockImplementation((params) => {
    callLog.push(params);
    return { data, isLoading: false, callLog };
  });
  return callLog;
}

describe('JobsReview — filter wiring + pagination', () => {
  beforeEach(() => {
    useJobs.mockReset();
  });

  it('mounts the FilterBar with variant="jobs" and the page state machine', () => {
    stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    expect(screen.getByPlaceholderText(/ML engineer/i)).toBeInTheDocument();
    expect(screen.getByText(/Jobs Review Queue/i)).toBeInTheDocument();
  });

  it('sends page=1 + page_size=50 on first mount with no filters', () => {
    const callLog = stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    expect(callLog.at(-1)).toMatchObject({ page: 1, page_size: 50 });
  });

  it('omits the status param when zero statuses are active (all)', () => {
    const callLog = stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    expect(callLog.at(-1)).not.toHaveProperty('status');
  });

  it('toggling a status pill sends status=<single> on the next useJobs call', async () => {
    const callLog = stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    await user.click(screen.getByRole('button', { name: /approved/i }));
    await waitFor(() => {
      const last = callLog.at(-1);
      expect(last.status).toBe('approved');
      // And page must have reset to 1.
      expect(last.page).toBe(1);
    });
  });

  it('toggling two status pills sends status=in_review,approved', async () => {
    const callLog = stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    await user.click(screen.getByRole('button', { name: /in review/i }));
    await user.click(screen.getByRole('button', { name: /approved/i }));
    await waitFor(() => {
      const last = callLog.at(-1);
      // The order depends on click order; just check the set.
      expect(['in_review', 'approved']).toContain(last.status.split(',')[0]);
      expect(['in_review', 'approved']).toContain(last.status.split(',')[1]);
    });
  });

  it('clicking Next increments page; clicking Prev decrements; both clamp at the edges', async () => {
    // 75 total jobs, page_size 50 → 2 pages (1 + 2).
    stubUseJobs({
      jobs: Array.from({ length: 25 }, (_, i) => ({ id: `seed-${i}`, status: 'in_review', title: 'x', company_name: 'y', url: 'z', ats_type: 'ashby' })),
      total: 75,
      page: 1,
      page_size: 50,
    });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    const next = screen.getByRole('button', { name: /Next/i });
    const prev = screen.getByRole('button', { name: /Prev/i });
    // On page 1: Prev disabled, Next enabled.
    expect(prev).toBeDisabled();
    expect(next).toBeEnabled();
    expect(screen.getByText(/Page 1 of 2/i)).toBeInTheDocument();
    // Click Next.
    await user.click(next);
    expect(screen.getByText(/Page 2 of 2/i)).toBeInTheDocument();
    // On page 2 (last): Next disabled, Prev enabled.
    expect(next).toBeDisabled();
    expect(prev).toBeEnabled();
    // Click Prev back to 1.
    await user.click(prev);
    expect(screen.getByText(/Page 1 of 2/i)).toBeInTheDocument();
  });

  it('any filter change resets page to 1 (Next is forgotten)', async () => {
    stubUseJobs({
      jobs: Array.from({ length: 50 }, (_, i) => ({ id: `seed-${i}`, status: 'in_review', title: 'x', company_name: 'y', url: 'z', ats_type: 'ashby' })),
      total: 200,
      page: 1,
      page_size: 50,
    });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    // Move to page 2 by clicking Next.
    await user.click(screen.getByRole('button', { name: /Next/i }));
    expect(screen.getByText(/Page 2 of 4/i)).toBeInTheDocument();
    // Type a keyword — should reset to page 1.
    await user.type(screen.getByPlaceholderText(/ML engineer/i), 'a');
    await waitFor(() => {
      expect(screen.getByText(/Page 1 of 4/i)).toBeInTheDocument();
    });
  });

  it('shows the "Showing N–M of T" counter on the second page', async () => {
    stubUseJobs({
      jobs: Array.from({ length: 3 }, (_, i) => ({ id: `seed-${i}`, status: 'in_review', title: 'x', company_name: 'y', url: 'z', ats_type: 'ashby' })),
      total: 100,
      page: 2,
      page_size: 50,
    });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    // Page 1 first → showing 1–3 of 100.
    expect(screen.getByText(/Showing 1–3 of 100/i)).toBeInTheDocument();
    // Click Next → page 2 → showing 51–53 of 100.
    await user.click(screen.getByRole('button', { name: /Next/i }));
    expect(screen.getByText(/Showing 51–53 of 100/i)).toBeInTheDocument();
  });

  it('does not send score_min/score_max when at the default (0, 100)', () => {
    const callLog = stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    const last = callLog.at(-1);
    expect(last).not.toHaveProperty('score_min');
    expect(last).not.toHaveProperty('score_max');
  });

  it('converts the score range 0-100 ints to 0.0-1.0 floats in the API params', () => {
    const callLog = stubUseJobs({ jobs: [], total: 0, page: 1, page_size: 50 });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <JobsReview />
      </QueryProvider>
    );
    // Mutate the score range via two range inputs — fire change events
    // directly. The min input has aria-label "Minimum score", the max
    // input has aria-label "Maximum score".
    const lo = screen.getByLabelText(/Minimum score/i);
    const hi = screen.getByLabelText(/Maximum score/i);
    fireInput(lo, '40');
    fireInput(hi, '90');
    // After both updates the last call should have score_min=0.4 and
    // score_max=0.9.
    waitFor(() => {
      const last = callLog.at(-1);
      expect(last.score_min).toBeCloseTo(0.4, 5);
      expect(last.score_max).toBeCloseTo(0.9, 5);
    });
  });
});

function fireInput(input, value) {
  // React controlled inputs need a descriptor-shaped event to set the
  // backing value, then a change event for React to pick it up.
  const proto = Object.getPrototypeOf(input);
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(input, value);
  fireEvent.change(input, { target: { value } });
}
