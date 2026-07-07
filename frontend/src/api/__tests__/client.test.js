import { describe, it, expect, vi, beforeEach } from 'vitest';

// -----------------------------------------------------------------------
// Hoisted spies. ``vi.mock('axios', factory)`` calls the factory at hoist
// time — well before the rest of the module body runs — so any vi.fn()
// we want shared between the factory and our test bodies MUST live
// inside vi.hoisted() so the references are stable across both phases.
//
// We mock the *module shape* that ``import axios from 'axios'`` sees:
//   axios.create(opts) -> instance { get, post, put, patch, delete }
// which mirrors axios v1's real surface. Only the verbs ``client.js``
// actually uses need to be vi.fn(); the rest are no-ops that we never
// call, so they're declared but never inspected.
// -----------------------------------------------------------------------
const { get, post, put, patch, del, instance } = vi.hoisted(() => {
  const get = vi.fn();
  const post = vi.fn();
  const put = vi.fn();
  const patch = vi.fn();
  const del = vi.fn();
  // Note: ``delete`` is a reserved word but valid as an identifier in
  // ES object-literal property names — string-equivalent to key
  // ``"delete"``.
  const instance = { get, post, put, patch, delete: del };
  return { get, post, put, patch, del, instance };
});

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => instance),
  },
}));

// Imports AFTER the mock so client.js picks up the mocked bindings.
import axios from 'axios';
import { triggerDiscovery, fetchSchedule } from '../client';

describe('api/client.js — HTTP method + URL wiring', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    put.mockReset();
    patch.mockReset();
    del.mockReset();
  });

  it('wires triggerDiscovery as axios.get (NOT axios.post) at /pipeline/discover', async () => {
    // Resolved envelope mimics what ``GET /api/pipeline/discover`` returns
    // when the boards scan completes successfully.
    const body = { status: 'completed', companies_attached: 7, scanned: 7 };
    get.mockResolvedValueOnce({ data: body });

    const result = await triggerDiscovery();

    // (1) POST must NOT have been called. This is the regression guard
    // the user spec asks for: a future flip back to api.post('/pipeline/discover')
    // will break this assertion loudly.
    expect(post).not.toHaveBeenCalled();
    // (2) DELETE/PUT/PATCH must never have been called.
    expect(del).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    expect(patch).not.toHaveBeenCalled();
    // (3) GET was called exactly once with the relative URL
    //     ``/pipeline/discover``. The full URL ${VITE_API_URL}/api/pipeline/discover
    //     is composed by axios from baseURL + this relative path; we pin the
    //     relative piece because that's what client.js actually controls.
    expect(get).toHaveBeenCalledTimes(1);
    // Trailing-slash tolerant: a future URL-normalizing helper that adds
    // ``/`` would be a no-op refactor and should not red-flag this test.
    expect(get.mock.calls[0][0].replace(/\/$/, '')).toBe('/pipeline/discover');
    // (4) Returned promise resolves to ``r.data`` (axios envelope unwrapped).
    expect(result).toEqual(body);
    // (5) Sanity: the shared axios instance was created via axios.create with
    //     a baseURL ending in /api so baseURL composition cannot drift.
    expect(axios.create).toHaveBeenCalledTimes(1);
    const createOpts = axios.create.mock.calls[0][0];
    expect(createOpts.baseURL.endsWith('/api')).toBe(true);
  });

  it('also resolves fetchSchedule through axios.get and returns r.data', async () => {
    // Secondary pin for the same .then((r) => r.data) shape used by every
    // wrapper in client.js — catches a future mutation that accidentally
    // drops the .data unwrap.
    get.mockResolvedValueOnce({
      data: {
        interval_hours: 1,
        options: [1, 2, 4, 6, 12, 24],
        next_run: '2026-01-01T00:00:00Z',
        updated_at: null,
      },
    });

    const schedule = await fetchSchedule();

    expect(get).toHaveBeenCalledTimes(1);
    // Trailing-slash tolerant — see comment in the triggerDiscovery test.
    expect(get.mock.calls[0][0].replace(/\/$/, '')).toBe('/pipeline/schedule');
    // No data envelope leak: the consumer shape should match the body,
    // not {data: {...}}.
    expect(schedule).not.toHaveProperty('data');
    expect(schedule.interval_hours).toBe(1);
    expect(schedule.options).toEqual([1, 2, 4, 6, 12, 24]);
  });
});
