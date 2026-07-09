import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FilterBar from './FilterBar';

describe('FilterBar — jobs variant (new /api/jobs query params)', () => {
  const noop = () => {};

  it('renders the keyword input, ATS dropdown, and score range slider', () => {
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={noop}
      />
    );
    expect(screen.getByPlaceholderText(/ML engineer/i)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /ATS/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/Minimum score/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Maximum score/i)).toBeInTheDocument();
  });

  it('shows the 5 status pills and the "all statuses" hint when none are active', () => {
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={noop}
      />
    );
    expect(screen.getByRole('button', { name: /in review/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /approved/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /rejected/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /flagged/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /applied/i })).toBeInTheDocument();
    expect(screen.getByText(/all statuses/i)).toBeInTheDocument();
  });

  it('toggles a status pill on click and fires setFilters with the new array', async () => {
    const setFilters = vi.fn();
    const user = userEvent.setup();
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={setFilters}
      />
    );
    await user.click(screen.getByRole('button', { name: /approved/i }));
    expect(setFilters).toHaveBeenCalledTimes(1);
    // setFilters is invoked with the functional updater; with vi.fn we
    // can read the first call's argument. We pass the (prev) => next
    // function so the parent can compute on top of stale state — but
    // here the implementation writes a literal value. Either form
    // works; just assert the resulting array includes 'approved'.
    const lastArg = setFilters.mock.calls.at(-1)[0];
    const result = typeof lastArg === 'function' ? lastArg({ statuses: [] }) : lastArg;
    expect(result.statuses).toEqual(['approved']);
  });

  it('toggles a status pill off when clicked twice (round-trip)', async () => {
    const setFilters = vi.fn();
    const user = userEvent.setup();
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: ['approved'], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={setFilters}
      />
    );
    const approvedPill = screen.getByRole('button', { name: /approved/i });
    // Active pill has aria-pressed=true.
    expect(approvedPill).toHaveAttribute('aria-pressed', 'true');
    await user.click(approvedPill);
    const lastArg = setFilters.mock.calls.at(-1)[0];
    const result = typeof lastArg === 'function' ? lastArg({ statuses: ['approved'] }) : lastArg;
    expect(result.statuses).toEqual([]);
  });

  it('renders a "clear" button when ≥1 status is active', () => {
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: ['approved', 'rejected'], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={noop}
      />
    );
    expect(screen.getByRole('button', { name: /clear/i })).toBeInTheDocument();
  });

  it('clicking "clear" resets statuses to []', async () => {
    const setFilters = vi.fn();
    const user = userEvent.setup();
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: ['approved'], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={setFilters}
      />
    );
    await user.click(screen.getByRole('button', { name: /clear/i }));
    const lastArg = setFilters.mock.calls.at(-1)[0];
    const result = typeof lastArg === 'function' ? lastArg({ statuses: ['approved'] }) : lastArg;
    expect(result.statuses).toEqual([]);
  });

  it('shows the score range values inline (0% – 100% by default)', () => {
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={noop}
      />
    );
    expect(screen.getByText('0% – 100%')).toBeInTheDocument();
  });

  it('typing in the keyword input fires setFilters with the new value', async () => {
    const setFilters = vi.fn();
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={setFilters}
      />
    );
    // ``fireEvent.change`` with the cumulative value is more
    // reliable than ``user.type`` for controlled inputs whose
    // backing state is a ``vi.fn`` spy (the spy doesn't update
    // React state, so each keystroke sees an empty React state
    // and the cumulative-value accumulation that ``user.type``
    // relies on breaks).
    const input = screen.getByPlaceholderText(/ML engineer/i);
    fireEvent.change(input, { target: { value: 'rust' } });
    const lastArg = setFilters.mock.calls.at(-1)[0];
    const result = typeof lastArg === 'function' ? lastArg({ q: '' }) : lastArg;
    expect(result.q).toBe('rust');
  });

  it('selecting an ATS value fires setFilters with the ats_type field', async () => {
    const setFilters = vi.fn();
    const user = userEvent.setup();
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={setFilters}
      />
    );
    await user.selectOptions(screen.getByRole('combobox', { name: /ATS/i }), 'greenhouse');
    const lastArg = setFilters.mock.calls.at(-1)[0];
    const result = typeof lastArg === 'function' ? lastArg({ ats_type: '' }) : lastArg;
    expect(result.ats_type).toBe('greenhouse');
  });

  it('changing postedFrom / postedTo fires setFilters with the date strings', async () => {
    const setFilters = vi.fn();
    const user = userEvent.setup();
    render(
      <FilterBar
        variant="jobs"
        filters={{ q: '', statuses: [], ats_type: '', scoreMin: 0, scoreMax: 100, postedFrom: '', postedTo: '' }}
        setFilters={setFilters}
      />
    );
    // The two date inputs are siblings; locate by label association.
    const fromLabel = screen.getByText(/Posted from/i);
    const fromField = fromLabel.parentElement.querySelector('input[type="date"]');
    const toLabel = screen.getByText(/Posted to/i);
    const toField = toLabel.parentElement.querySelector('input[type="date"]');
    await user.type(fromField, '2026-01-01');
    await user.type(toField, '2026-12-31');
    expect(setFilters).toHaveBeenCalled();
    // The last call should have posted_to=2026-12-31.
    const lastArg = setFilters.mock.calls.at(-1)[0];
    const result = typeof lastArg === 'function' ? lastArg({ postedTo: '' }) : lastArg;
    expect(result.postedTo).toBe('2026-12-31');
    // And an earlier call should have posted_from=2026-01-01.
    const allCalls = setFilters.mock.calls.map((c) => {
      const arg = c[0];
      return typeof arg === 'function' ? arg({ postedFrom: '' }) : arg;
    });
    expect(allCalls.some((r) => r.postedFrom === '2026-01-01')).toBe(true);
  });
});

describe('FilterBar — default variant (CompanyFeed, unchanged)', () => {
  it('renders the v0.4 source/window/score/keyword controls', () => {
    render(
      <FilterBar
        category="boards"
        filters={{ source: '', delta_hours: '', min_score: 0, q: '' }}
        setFilters={() => {}}
      />
    );
    expect(screen.getByText('Source')).toBeInTheDocument();
    expect(screen.getByText('Window')).toBeInTheDocument();
    expect(screen.getByText('Min Score')).toBeInTheDocument();
    expect(screen.getByText('Keyword')).toBeInTheDocument();
  });
});
