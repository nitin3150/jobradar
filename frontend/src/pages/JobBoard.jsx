import { useMemo, useState } from 'react';
import { useCreateApplication } from '../hooks/useApplications';
import { useJobStatus, useJobs } from '../hooks/useJobs';
import { useResearchMutation } from '../hooks/useResearch';
import JobBoardFilters from '../components/JobBoardFilters';
import JobCard from '../components/JobCard';
import InterviewPrepModal from '../components/InterviewPrepModal';
import OutreachPanel from '../components/OutreachPanel';

const DEFAULT_PAGE_SIZE = 20;
const DEFAULT_FILTERS = { q: undefined, status: undefined, ats_type: undefined, score_min: 0, posted_from: undefined, posted_to: undefined, sort: 'deadline_asc' };

/**
 * The merged Job Board page.
 *
 * This is the operator's primary workspace for managing inbound
 * board-sourced opportunities. It absorbs two earlier surfaces into
 * one:
 *
 * - The Dashboard's "Job Boards" tab — a feed of scored jobs that
 *   used to render via :class:`CompanyCard` and offered only a
 *   "Generate Outreach" affordance.
 * - The JobsReview page — a status-filtered queue with Approve /
 *   Reject / Mark Applied buttons per row.
 *
 * The new shape recognises that those two surfaces were operating
 * over the *same* Supabase ``jobs`` table, just with different
 * affordances. We now expose everything: the filter bar lets the
 * operator narrow the queue (search, status, board, score, dates),
 * pagination keeps it scrollable, the status dropdown on each card
 * drives ``useJobStatus`` so a status change writes a
 * ``job_status_history`` row on the server, and the **Interview
 * Prep** button blocks-on-return of the sync
 * ``POST /api/jobs/{id}/research`` call so the operator can read
 * the brief immediately.
 *
 * Two ``useState`` cells hold the modal + slide-out selections.
 * Clicking a status dropdown selection fires the mutation directly
 * (no modality), but Interview Prep / Generate Outreach open
 * resptive overlay surfaces.
 */
export default function JobBoard() {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [prepJob, setPrepJob] = useState(null);
  const [outreachJob, setOutreachJob] = useState(null);

  // Build the query-string dict from current filters. ``status`` is
  // an array on purpose but the backend ``GET /api/jobs`` route
  // expects a SINGLE comma-separated string (``?status=in_review,
  // approved``) per the FastAPI ``Query(alias='status')`` shape.
  // Joining here is the only correct path — axios would otherwise
  // serialise the array to ``?status=in_review&status=approved`` and
  // FastAPI would silently drop all but the first value, making
  // multi-select filtering a no-op. Score range, dates, and
  // ats_type map 1:1.
  //
  // v0.5: the JobBoardFilters input converts the percent-range
  // slider (0-100) to a 0.0-1.0 float at the boundary, so we can
  // pass ``filters.score_min`` straight through. ``sort`` is
  // also forwarded verbatim — the backend defaults to
  // ``deadline_asc`` when the param is missing or unknown.
  const queryFilters = useMemo(() => {
    const out = {
      page,
      page_size: pageSize,
      sort: filters.sort || 'deadline_asc',
    };
    // ``score_min`` is only included when non-zero so the URL
    // doesn't carry a noisy ``?score_min=0`` on every request —
    // the backend's ``if score_min > 0.0`` guard already short-
    // circuits a no-op filter, but emitting it makes the
    // DevTools Network panel look like the filter is active.
    // ``DEFAULT_FILTERS`` initialises ``score_min: 0`` and nothing
    // in the codebase ever sets it to ``undefined``/``null``, so
    // it's always a number — no ``?? 0`` needed.
    if (filters.score_min > 0) out.score_min = filters.score_min;
    if (filters.q) out.q = filters.q;
    if (filters.ats_type) out.ats_type = filters.ats_type;
    if (filters.posted_from) out.posted_from = filters.posted_from;
    if (filters.posted_to) out.posted_to = filters.posted_to;
    if (filters.status && filters.status.length > 0) {
      // Comma-joined single string for the FastAPI ``status``
      // query param. The route splits on ``,`` and applies an
      // ``IN (...)`` predicate; the single-vs-multi wire shape
      // is the same on the SQL side.
      out.status = filters.status.join(',');
    }
    return out;
  }, [filters, page, pageSize]);

  const jobsQuery = useJobs(queryFilters);
  const jobs = jobsQuery.data?.jobs || [];
  const total = jobsQuery.data?.total ?? 0;

  const jobStatus = useJobStatus();
  const createApplication = useCreateApplication();
  const research = useResearchMutation();

  // Handlers hoisted up so the cards render responsibly. Each fires
  // the relevant mutation / state-transitions; callers do NOT need
  // to know about React Query.
  const handleChangeStatus = (job, nextStatus) => {
    jobStatus.mutate({ id: job.id, status: nextStatus, source: 'user' });
  };
  const handleInterviewPrep = (job) => setPrepJob(job);
  const handleGenerateOutreach = (job) => setOutreachJob(job);
  const handleMarkApplied = (job) => {
    // Mirror the OLD JobsReview behaviour: open the job URL in a new
    // tab so the operator can complete the external apply, then POST
    // /api/applications to flip the Job status from
    // 'approved' → 'applied' atomically. The 0ms deferral keeps the
    // popup inside the click's gesture window (Chrome suppresses
    // popups not triggered by a user gesture).
    if (!job?.url) return;
    setTimeout(() => {
      window.open(job.url, '_blank', 'noopener,noreferrer');
    }, 0);
    createApplication.mutate({ jobId: job.id, notes: null });
  };

  const handleFiltersChange = (next) => setFilters(next);
  const handlePageSizeChange = (size) => {
    setPageSize(size);
    setPage(1);
  };

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const isLoadingFirstPage = jobsQuery.isLoading && jobs.length === 0;

  return (
    <div className="max-w-6xl mx-auto px-4 py-6">
      <header className="flex items-end justify-between mb-4 gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Job Board</h1>
          <p className="text-sm text-gray-500 mt-1">
            Scored engineering roles from Ashby, Greenhouse, and Lever. Search, filter by status / board / score / posted-date range, change status inline, and run an LLM-synthesised interview-prep brief on any card.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {jobStatus.isPending && (
            <span className="text-xs text-gray-500 flex items-center gap-2">
              <span className="w-3 h-3 bg-indigo-500 rounded-full animate-pulse" />
              Updating status…
            </span>
          )}
          <button
            type="button"
            onClick={() => setFilters(DEFAULT_FILTERS)}
            className="text-xs px-3 py-1.5 text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
          >
            Reset filters
          </button>
        </div>
      </header>

      <JobBoardFilters
        filters={filters}
        onChange={handleFiltersChange}
        pageSize={pageSize}
        onPageSizeChange={handlePageSizeChange}
        total={total}
      />

      <div className="bg-white border border-gray-200 rounded-xl px-4 py-3 mb-4 text-sm text-gray-600 flex items-center justify-between">
        <span>
          Showing{' '}
          <span className="font-semibold text-gray-900">
            {jobs.length}
          </span>{' '}
          of <span className="font-semibold text-gray-900">{total}</span> jobs
          {page > 1 && ` (page ${page} / ${totalPages})`}
        </span>
        {jobStatus.isError && (
          <span className="text-xs text-red-600">Status update failed — retry?</span>
        )}
      </div>

      {isLoadingFirstPage && (
        <div className="bg-white border border-gray-200 rounded-xl p-12 text-center text-gray-500">
          <span className="inline-block w-6 h-6 border-2 border-indigo-200 border-t-indigo-600 rounded-full animate-spin mb-3 align-middle" />
          <p>Loading jobs…</p>
        </div>
      )}

      {!isLoadingFirstPage && jobs.length === 0 && (
        <div className="bg-white border border-gray-200 rounded-xl p-12 text-center">
          <p className="text-gray-500 text-lg">No jobs match these filters.</p>
          <p className="text-gray-400 text-sm mt-2">
            Reset filters or wait for the next boards-scan to land more rows.
          </p>
        </div>
      )}

      <div className="space-y-4">
        {jobs.map((job) => (
          <JobCard
            key={job.id}
            job={job}
            onChangeStatus={handleChangeStatus}
            onInterviewPrep={handleInterviewPrep}
            onGenerateOutreach={handleGenerateOutreach}
            onMarkApplied={job.status === 'approved' ? handleMarkApplied : undefined}
          />
        ))}
      </div>

      {totalPages > 1 && (
        <Pagination
          page={page}
          totalPages={totalPages}
          onChange={(p) => {
            setPage(p);
            window.scrollTo({ top: 0, behavior: 'smooth' });
          }}
        />
      )}

      <InterviewPrepModal
        open={Boolean(prepJob)}
        job={prepJob}
        mutation={research}
        onClose={() => setPrepJob(null)}
      />

      {outreachJob && (
        <>
          <div
            className="fixed inset-0 bg-black/20 z-40"
            onClick={() => setOutreachJob(null)}
          />
          <OutreachPanel
            company={{
              ...outreachJob,
              name: outreachJob.company_name,
              company_summary: outreachJob.ai_fit_reasoning,
            }}
            onClose={() => setOutreachJob(null)}
          />
        </>
      )}
    </div>
  );
}

function Pagination({ page, totalPages, onChange }) {
  // Compact pagination: Prev / [1 … N] / Next. Cap the rendered
  // range at 7 page numbers with ellipsis so a 50-page corpus
  // doesn't blow the layout. Click prevention on the active page
  // avoids a redundant network roundtrip.
  const pages = computePageRange(page, totalPages);
  return (
    <nav className="flex items-center gap-1 mt-6 justify-center" aria-label="Pagination">
      <button
        type="button"
        disabled={page <= 1}
        onClick={() => onChange(page - 1)}
        className="px-3 py-1.5 text-sm rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        ← Prev
      </button>
      {pages.map((p, idx) =>
        p === '…' ? (
          <span key={`gap-${idx}`} className="px-2 text-gray-400 text-sm">…</span>
        ) : (
          <button
            key={p}
            type="button"
            onClick={() => onChange(p)}
            aria-current={p === page ? 'page' : undefined}
            className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${
              p === page
                ? 'bg-indigo-600 text-white border-indigo-600'
                : 'bg-white text-gray-700 border-gray-200 hover:bg-gray-50'
            }`}
          >
            {p}
          </button>
        ),
      )}
      <button
        type="button"
        disabled={page >= totalPages}
        onClick={() => onChange(page + 1)}
        className="px-3 py-1.5 text-sm rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        Next →
      </button>
    </nav>
  );
}

// Returns the visible page numbers for a compact pagination strip.
// Always shows page 1, page N, the active page ± 1, and ellipses
// between the gaps. A 7-page corpus shows 1..7 with no ellipses;
// a 50-page corpus on page 17 shows 1 … 16 17 18 … 50.
function computePageRange(page, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const out = new Set([1, total, page, page - 1, page + 1]);
  out.delete(0);
  out.delete(total + 1);
  const sorted = Array.from(out).filter((n) => n >= 1 && n <= total).sort((a, b) => a - b);
  const withGaps = [];
  for (let i = 0; i < sorted.length; i++) {
    withGaps.push(sorted[i]);
    if (i < sorted.length - 1 && sorted[i + 1] - sorted[i] > 1) {
      withGaps.push('…');
    }
  }
  return withGaps;
}
