import { useMemo, useState } from 'react';
import { useCreateApplication } from '../hooks/useApplications';
import { useApproveJob, useJobs, useRejectJob } from '../hooks/useJobs';
import FilterBar from '../components/FilterBar';

const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  applied: 'bg-blue-100 text-blue-800',
  flagged: 'bg-orange-100 text-orange-800',
};

function TimeRemaining({ deadline }) {
  if (!deadline) return null;
  const ms = new Date(deadline) - new Date();
  if (ms <= 0) return <span className="text-red-500 text-xs">Expired</span>;
  const hrs = Math.floor(ms / 3600000);
  const mins = Math.floor((ms % 3600000) / 60000);
  return (
    <span className="text-xs text-gray-500">
      {hrs}h {mins}m remaining
    </span>
  );
}

// ``Mark as applied`` is a single-click handoff: open the job URL in a
// new tab so the operator can apply externally, then fire the
// ``POST /api/applications`` mutation that atomically flips the Job
// status from ``approved`` → ``applied`` and creates the
// ``Application(submitted)`` row. The two actions are co-located in
// one handler so a click that opens the URL but then bails on the
// mutation is impossible — the operator either commits the handoff
// (URL opens + row created) or nothing happens.
//
// The window.open call is wrapped in a ``setTimeout(..., 0)`` so the
// ``noopener,noreferrer`` security flags take effect BEFORE the
// ``useMutation`` hook's React state update lands. Without the
// timeout, browsers that batch the new-tab opening and the React
// state update can cancel the popup (Chrome blocks popups that
// aren't "directly triggered" by a user gesture event).
function handleMarkAsApplied(job, createApplication) {
  if (!job?.url) return;
  // 1. Open the job URL in a new tab. The 0-ms deferral keeps the
  //    popup inside the click event's gesture window so popup
  //    blockers (Chrome) don't suppress it.
  setTimeout(() => {
    window.open(job.url, '_blank', 'noopener,noreferrer');
  }, 0);
  // 2. Fire the POST /api/applications mutation. On success the
  //    useCreateApplication onSuccess handler invalidates the
  //    jobs + applications caches, so this card's status flips to
  //    'applied' and the new row appears in ApplicationTracker.
  createApplication.mutate({ jobId: job.id, notes: null });
}

// All 5 valid JobStatus enum members. Source of truth mirrors
// ``JobStatus`` Literal in ``backend/routes/jobs.py``; the order
// here is the order the pill bar renders them.
const ALL_STATUSES = ['in_review', 'approved', 'rejected', 'flagged', 'applied'];

// Build the /api/jobs query params from the held filter state. The
// omission rules match the backend's defaults: any filter that would
// be a no-op is dropped from the URL so React Query keys stay
// short and the backend's filter-then-skip path is taken.
function buildApiParams(filters, page, pageSize) {
  const params = { page, page_size: pageSize };
  if (filters.q) params.q = filters.q;
  if (filters.statuses.length > 0 && filters.statuses.length < ALL_STATUSES.length) {
    params.status = filters.statuses.join(',');
  }
  if (filters.ats_type) params.ats_type = filters.ats_type;
  if (filters.scoreMin > 0) params.score_min = filters.scoreMin / 100;
  if (filters.scoreMax < 100) params.score_max = filters.scoreMax / 100;
  if (filters.postedFrom) params.posted_from = filters.postedFrom;
  if (filters.postedTo) params.posted_to = filters.postedTo;
  return params;
}

const PAGE_SIZE = 50;

export default function JobsReview() {
  // Filter state + page are held in two separate ``useState``s and
  // combined in ``buildApiParams``. Any filter change resets page to 1
  // so the operator doesn't end up looking at page 5 of a query that
  // now has 1 page total. Page changes preserve the filters.
  const [filters, setFilters] = useState({
    q: '',
    statuses: [],
    ats_type: '',
    scoreMin: 0,
    scoreMax: 100,
    postedFrom: '',
    postedTo: '',
  });
  const [page, setPage] = useState(1);

  // FilterBar edits route through this wrapper so a single change
  // resets the page index in lockstep — the alternative (separate
  // ``useEffect``s) risks a stale render where ``page`` still points
  // at a now-invalid offset.
  const updateFilters = (next) => {
    setFilters(typeof next === 'function' ? next : { ...filters, ...next });
    setPage(1);
  };

  const apiParams = useMemo(
    () => buildApiParams(filters, page, PAGE_SIZE),
    [filters, page],
  );
  const { data, isLoading } = useJobs(apiParams);
  const approve = useApproveJob();
  const reject = useRejectJob();
  const createApplication = useCreateApplication();

  const total = data?.total ?? 0;
  const jobs = data?.jobs ?? [];
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const showingFrom = jobs.length > 0 ? (page - 1) * PAGE_SIZE + 1 : 0;
  const showingTo = showingFrom + jobs.length - 1;

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Jobs Review Queue</h1>
        </div>

        <FilterBar
          variant="jobs"
          filters={filters}
          setFilters={updateFilters}
        />

        {isLoading && (
          <div className="text-center py-12 text-gray-500">Loading jobs...</div>
        )}

        {!isLoading && jobs.length === 0 && (
          <div className="text-center py-12 text-gray-400">No jobs match the current filters.</div>
        )}

        <div className="space-y-4">
          {jobs.map((job) => (
            <div
              key={job.id}
              className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${
                        STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700'
                      }`}
                    >
                      {job.status.replace('_', ' ')}
                    </span>
                    <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                      {job.ats_type}
                    </span>
                    {job.ai_fit_score != null && (
                      <span className="text-xs font-medium text-indigo-600">
                        {Math.round(job.ai_fit_score * 100)}% fit
                      </span>
                    )}
                  </div>
                  <h3 className="text-lg font-semibold text-gray-900 truncate">
                    {job.title}
                  </h3>
                  <p className="text-sm text-gray-500 mt-0.5">{job.company_name}</p>
                  {job.ai_fit_reasoning && (
                    <p className="text-sm text-gray-600 mt-2 line-clamp-2">
                      {job.ai_fit_reasoning}
                    </p>
                  )}
                  <div className="flex items-center gap-4 mt-2">
                    <TimeRemaining deadline={job.review_deadline} />
                    <a
                      href={job.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-indigo-500 hover:underline"
                    >
                      View posting →
                    </a>
                  </div>
                </div>

                {job.status === 'in_review' && (
                  <div className="flex gap-2 ml-4 shrink-0">
                    <button
                      onClick={() => approve.mutate(job.id)}
                      disabled={approve.isPending}
                      className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => reject.mutate(job.id)}
                      disabled={reject.isPending}
                      className="px-4 py-2 bg-red-100 text-red-700 text-sm font-medium rounded-lg hover:bg-red-200 disabled:opacity-50 transition-colors"
                    >
                      Reject
                    </button>
                  </div>
                )}
                {job.status === 'approved' && (
                  <div className="flex gap-2 ml-4 shrink-0">
                    <button
                      onClick={() => handleMarkAsApplied(job, createApplication)}
                      disabled={createApplication.isPending}
                      title="Opens the job URL in a new tab and records this as an application on the tracker."
                      className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                    >
                      {createApplication.isPending ? 'Marking…' : 'Mark as applied'}
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Pagination — prev/next + counter. ``Page X of Y`` keeps the
            small N case (1-2 pages) intuitive. Prev is disabled on
            page 1; Next is disabled on the last page. Both buttons
            stay mounted (with ``disabled`` styling) so the layout
            doesn't jump. */}
        {!isLoading && total > 0 && (
          <div className="flex items-center justify-between mt-6 text-sm text-gray-600">
            <span>
              Showing {showingFrom}–{showingTo} of {total}
            </span>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="px-3 py-1 border border-gray-300 rounded-lg text-xs font-medium hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                ← Prev
              </button>
              <span className="tabular-nums">
                Page {page} of {totalPages}
              </span>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={page >= totalPages}
                className="px-3 py-1 border border-gray-300 rounded-lg text-xs font-medium hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Next →
              </button>
            </div>
          </div>
        )}
    </div>
  );
}
