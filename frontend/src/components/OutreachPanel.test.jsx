import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import OutreachPanel from './OutreachPanel';

// ---------------------------------------------------------------------------
// IMPORTANT: this suite pins the *observable behavior* of the
// render-phase "adjust state when a prop changes" pattern in OutreachPanel:
//
//   const [lastCompanyId, setLastCompanyId] = useState(company?.id ?? null);
//   if (currentCompanyId !== lastCompanyId) {
//     setLastCompanyId(currentCompanyId);
//     setGeneratedMessage('');
//   }
//
// The lint rule `react-hooks/set-state-in-effect` is the ACTUAL tripwire
// for the regression: a future refactor that swaps this out for
//   useEffect(() => setGeneratedMessage(''), [company?.id])
// produces identical observable behavior, so these tests alone cannot
// detect the regression. The inline comment is intentional — do NOT
// "helpfully" add a same-id "message is preserved" test back; it would
// give future maintainers a false sense that the test suite defends the
// pattern. ESLint is the contract.
// ---------------------------------------------------------------------------

// Mock the outreach mutation hook so the component doesn't hit React Query's
// real network/cache wiring — we only care about UI behavior.
vi.mock('../hooks/useOutreach', () => ({
  useGenerateOutreach: vi.fn(),
}));

import { useGenerateOutreach } from '../hooks/useOutreach';

const companyA = {
  id: 'company-a',
  name: 'Acme Corp',
  company_summary: 'Acme is great.',
  hiring_signals: ['Hiring backend engineers'],
};
const companyB = {
  id: 'company-b',
  name: 'Beta Inc',
  company_summary: 'Beta is hiring.',
  hiring_signals: [],
};

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

describe('OutreachPanel — render-phase state reset on company change', () => {
  beforeEach(() => {
    localStorage.clear();
    // `mockReset` clears both call history and implementation; each test sets
    // its own `mockReturnValue` so leakage between tests is impossible.
    useGenerateOutreach.mockReset();
  });

  it('renders the active company name in the header', () => {
    useGenerateOutreach.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
      isError: false,
    });
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <OutreachPanel company={companyA} onClose={() => {}} />
      </QueryProvider>
    );
    expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    expect(screen.getByText(/Acme is great/)).toBeInTheDocument();
  });

  it('renders nothing when company is null', () => {
    useGenerateOutreach.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
      isError: false,
    });
    const { QueryProvider } = makeWrapper();
    const { container } = render(
      <QueryProvider>
        <OutreachPanel company={null} onClose={() => {}} />
      </QueryProvider>
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows a freshly generated message after the user clicks Generate', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ content: 'Hello from A' });
    useGenerateOutreach.mockReturnValue({
      mutateAsync,
      isPending: false,
      isError: false,
    });
    const user = userEvent.setup();
    const { QueryProvider } = makeWrapper();
    render(
      <QueryProvider>
        <OutreachPanel company={companyA} onClose={() => {}} />
      </QueryProvider>
    );

    await user.click(screen.getByRole('button', { name: /Generate Message/i }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Hello from A')).toBeInTheDocument()
    );
  });

  it('clears the previously generated message when the company prop changes', async () => {
    // First mount: company A, generate a message.
    const mutateAsync = vi.fn().mockResolvedValue({ content: 'Message for A' });
    useGenerateOutreach.mockReturnValue({
      mutateAsync,
      isPending: false,
      isError: false,
    });
    const user = userEvent.setup();
    const { QueryProvider, qc } = makeWrapper();
    const { rerender } = render(
      <QueryProvider>
        <OutreachPanel company={companyA} onClose={() => {}} />
      </QueryProvider>
    );
    await user.click(screen.getByRole('button', { name: /Generate Message/i }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Message for A')).toBeInTheDocument()
    );

    // Now switch to company B — message MUST clear (render-phase guard fires
    // because the id changed). Use the same `qc` so React Query state is
    // preserved across this prop update.
    mutateAsync.mockResolvedValue({ content: 'Message for B' });
    rerender(
      <QueryClientProvider client={qc}>
        <OutreachPanel company={companyB} onClose={() => {}} />
      </QueryClientProvider>
    );
    await waitFor(() =>
      expect(screen.queryByDisplayValue('Message for A')).toBeNull()
    );
    expect(screen.getByText('Beta Inc')).toBeInTheDocument();
  });

  it('does not crash when company changes from defined to undefined', () => {
    useGenerateOutreach.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
      isError: false,
    });
    const { QueryProvider, qc } = makeWrapper();
    const { container, rerender } = render(
      <QueryProvider>
        <OutreachPanel company={companyA} onClose={() => {}} />
      </QueryProvider>
    );
    expect(() => {
      rerender(
        <QueryClientProvider client={qc}>
          <OutreachPanel company={undefined} onClose={() => {}} />
        </QueryClientProvider>
      );
    }).not.toThrow();
    // The component early-returns null when company is falsy.
    expect(container.firstChild).toBeNull();
  });
});
