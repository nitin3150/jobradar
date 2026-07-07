import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// -----------------------------------------------------------------------
// Mock the three axios-backed wrapper functions ScheduleControl imports
// from ``../api/client``. We do NOT mock axios itself — wrapping the
// thin client adapters keeps the test honest about what the component
// actually depends on, and the failure-shape tests below can fabricate
// whatever they need (an AxiosError-like rejection, or a
// status="failed" envelope) without spinning up a fake server.
//
// Pattern note: the ``vi.fn()`` calls MUST live inside the factory to
// survive vitest's module-mock hoisting — referencing outer-scope vi.fn
// variables directly from inside the factory triggers a
// "Cannot access X before initialization" error at hoist time. This
// mirrors the convention in `OutreachPanel.test.jsx`.
// -----------------------------------------------------------------------
// Note: this test lives under ``__tests__/`` (one level deeper than the
// co-located OutreachPanel.test.jsx). The api/client module is two
// directories up, ScheduleControl is one up.
vi.mock('../../api/client', () => ({
  fetchSchedule: vi.fn(),
  updateSchedule: vi.fn(),
  triggerDiscovery: vi.fn(),
}));

// Imports AFTER the mock so ScheduleControl picks up the mocked bindings.
import { fetchSchedule, updateSchedule, triggerDiscovery } from '../../api/client';
import ScheduleControl from '../ScheduleControl';

// Default schedule so useQuery resolves cleanly and the render-phase
// ``isLoading`` flag is not stuck true during the click.
const DEFAULT_SCHEDULE = {
  interval_hours: 1,
  options: [1, 2, 4, 6, 12, 24],
  next_run: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  function Wrapper({ children }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

// Build a minimal AxiosError-shaped rejection. The component reads
// only ``e?.response?.status`` (for the 409 branch) and ``e.message``
// (for the fallback branch), so we don't need a real AxiosError
// instance — a plain ``Error`` augmented with ``response`` is enough.
function axiosLikeReject(status, message) {
  const err = new Error(message ?? `Request failed with status code ${status}`);
  err.response = { status, data: null, statusText: '' };
  err.isAxiosError = true;
  return err;
}

describe('ScheduleControl — discover error UX', () => {
  beforeEach(() => {
    fetchSchedule.mockReset();
    updateSchedule.mockReset();
    triggerDiscovery.mockReset();
    fetchSchedule.mockResolvedValue(DEFAULT_SCHEDULE);
    updateSchedule.mockResolvedValue(DEFAULT_SCHEDULE);
  });

  it('shows the "already running" copy on a 409 Conflict response', async () => {
    triggerDiscovery.mockRejectedValue(axiosLikeReject(409));

    const Wrapper = makeWrapper();
    render(
      <Wrapper>
        <ScheduleControl />
      </Wrapper>
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Discover boards/i }));

    // Pinned copy — keeping this exact string is intentional, so a
    // future "make the message friendlier" rewrite fails loudly here.
    await waitFor(() =>
      expect(
        screen.getByText('Pipeline already running — try again in a moment.')
      ).toBeInTheDocument()
    );
    // And the captured mutation call actually ran once — guards against
    // a regression where the click handler short-circuits and never
    // invokes triggerDiscovery at all.
    expect(triggerDiscovery).toHaveBeenCalledTimes(1);
  });

  it('falls through to "Failed: <e.message>" on non-409 errors', async () => {
    triggerDiscovery.mockRejectedValue(
      axiosLikeReject(500, 'Internal Server Error')
    );

    const Wrapper = makeWrapper();
    render(
      <Wrapper>
        <ScheduleControl />
      </Wrapper>
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Discover boards/i }));

    await waitFor(() =>
      expect(screen.getByText('Failed: Internal Server Error')).toBeInTheDocument()
    );
    // The 409 copy must NOT appear for any non-409 status.
    expect(
      screen.queryByText('Pipeline already running — try again in a moment.')
    ).toBeNull();
  });

  it('falls through on network errors (e.response is undefined)', async () => {
    // Real axios ERR_NETWORK rejections have no ``response`` key. The
    // optional-chained guard in the component must let this case reach
    // the fallback branch, not crash.
    triggerDiscovery.mockRejectedValue(new Error('Network Error'));

    const Wrapper = makeWrapper();
    render(
      <Wrapper>
        <ScheduleControl />
      </Wrapper>
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Discover boards/i }));

    await waitFor(() =>
      expect(screen.getByText('Failed: Network Error')).toBeInTheDocument()
    );
    expect(
      screen.queryByText('Pipeline already running — try again in a moment.')
    ).toBeNull();
  });

  it('shows "Failed: <error>" on a 200 response with status="failed"', async () => {
    triggerDiscovery.mockResolvedValue({
      status: 'failed',
      companies_attached: 0,
      scanned: 0,
      error: 'rate limited',
    });

    const Wrapper = makeWrapper();
    render(
      <Wrapper>
        <ScheduleControl />
      </Wrapper>
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Discover boards/i }));

    await waitFor(() =>
      expect(screen.getByText('Failed: rate limited')).toBeInTheDocument()
    );
    // Defensive: the 409 copy must NOT leak into the success branch.
    expect(
      screen.queryByText('Pipeline already running — try again in a moment.')
    ).toBeNull();
  });

  it('shows the success copy on a 200 response with status="completed"', async () => {
    triggerDiscovery.mockResolvedValue({
      status: 'completed',
      companies_attached: 7,
      scanned: 12,
    });

    const Wrapper = makeWrapper();
    render(
      <Wrapper>
        <ScheduleControl />
      </Wrapper>
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Discover boards/i }));

    await waitFor(() =>
      expect(screen.getByText('Attached 7 new companies')).toBeInTheDocument()
    );
  });
});
