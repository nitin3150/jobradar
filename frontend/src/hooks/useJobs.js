import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  approveJob,
  fetchJob,
  fetchJobs,
  fetchPendingCount,
  patchJobStatus,
  rejectJob,
} from '../api/jobs';
// Note: the previous ``import { api as _api } from '../api/client';`` was
// removed because ``client.js`` exposes the axios instance as its DEFAULT
// export (named exports are only the wrapper functions like
// ``fetchCompanies``). The variable was unused dead code in this module
// anyway — every actual call routes through ``fetchJobs`` /
// ``patchJobStatus`` above. Keeping it would block Vite/Rolldown builds
// (Render deploy error: ``MISSING_EXPORT: "api" is not exported by
// "src/api/client.js"``).

export function useJobs(filters = {}) {
  // The filter object is the React Query key — identical objects
  // share the cache, so flipping page or score range stays
  // deterministic. ``staleTime: 30s`` keeps the cards fresh without
  // hammering Postgres on every keystroke into the search box.
  return useQuery({
    queryKey: ['jobs', filters],
    queryFn: () => fetchJobs(filters),
    staleTime: 30_000,
    refetchInterval: 60_000,
    keepPreviousData: true,
  });
}

// Single-job lookup. Drives the React ``JobDetail`` page; mirrors
// the pattern of ``useCompany`` in ``hooks/useCompanies.js``. The
// ``enabled: !!id`` guard avoids a wasted request on first render
// when ``useParams`` returns ``{id: undefined}`` (deep-link
// mid-route-mount).
export function useJob(id) {
  return useQuery({
    queryKey: ['job', id],
    queryFn: () => fetchJob(id),
    enabled: !!id,
    // 30s is short enough to catch a status flip the operator
    // makes from the JobBoard + nav-back, long enough to skip
    // re-fetch storms when the React Query cache is warm.
    staleTime: 30_000,
  });
}

// Number of ``approved`` rows — the auto-apply queue size. Drives
// the Navbar badge (``useApprovedCount → Navbar badge wire``) so
// the operator sees at-a-glance "how many jobs still need an
// application submitted". Renamed from ``usePendingCount`` after
// the v0.6 single-threshold scoring flip: previously this counted
// the operator-review queue (``status == "in_review"``); that
// pool no longer exists post-flip, only the auto-apply queue
// (``status == "approved"``) is meaningful. The route name
// ``/api/jobs/pending-count`` was kept to avoid breaking
// bookmarked URLs + audit-log aliases.
export function useApprovedCount() {
  return useQuery({
    queryKey: ['jobs', 'approved-count'],
    queryFn: fetchPendingCount,
    refetchInterval: 30_000,
  });
}

// Generic status PATCH — replaces the per-status POST endpoints. The
// body is forwarded verbatim to /api/jobs/{id}/status so the backend
// state-machine allows the transition and writes a job_status_history
// row in the same tx.
//
// Accepts an OPTIONAL ``onError`` callback so the caller can surface
// failures (race-condition 409, network drop, backend 5xx) without
// blocking on React Query's default silent rejection. Cache
// invalidation on success is preserved regardless of caller-supplied
// callbacks — the hook composes on top of them rather than being
// shadowed by them.
export function useJobStatus({ onError: onErrorCallback } = {}) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status, source, note }) =>
      patchJobStatus(id, { status, source, note }),
    onSuccess: () => {
      // Prefix match on ``['jobs']`` invalidates BOTH the list + the
      // pending-count badge; the latter drives the Navbar counter so
      // it must refresh whenever a row's status changes.
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
    onError: (err, variables, ctx) => {
      onErrorCallback?.(err, variables, ctx);
    },
  });
}

// Legacy approve / reject — kept so the Dashboard's PendingReviewWidget
// (which still mounts with the old pattern) doesn't break mid-migration.
// Will be removed once the widget is rewritten to use useJobStatus.
export function useApproveJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: approveJob,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}

export function useRejectJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: rejectJob,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}
