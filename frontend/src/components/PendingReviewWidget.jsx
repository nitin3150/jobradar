import { useCreateApplication } from '../hooks/useApplications';
import { useApproveJob, useJobs, useRejectJob } from '../hooks/useJobs';

// ---------------------------------------------------------------------------
// ``PendingReviewWidget`` — a compact job-lifecycle panel that surfaces the
// actionable subset of the ``jobs`` table directly on the Dashboard:
//
// * ``status == 'in_review'``: AI-scored winners dropped in by the hourly
//   boards scan that need a human Approve/Reject decision before the
//   review_deadline expires (matching the badges the Navbar already shows).
// * ``status == 'approved'``: decisions the operator has already made —
//   one click on the primary CTA opens the posting in a new tab AND
//   fires ``POST /api/applications`` (see ``handleApply``) so the row
//   lands in ApplicationTracker with status='submitted'.
//
// The widget hides when there is nothing to act on so a quiet Dashboard
// (all jobs decided + applied) stays scanner-focused instead of showing
// an empty card with zero rows. The two visual sub-sections ("To review"
// vs "Ready to apply") split the two action surface areas so the Apply
// CTA — the operator's main hot path — sits below the rejection bucket.
//
// Data sourcing: TWO parallel ``useJobs`` queries (one per status). We do
// not filter server-side for ``score DESC`` here because the backend's
// default ordering is good enough for top-N review (the AI-fit score is
// surfaced inline per row so the operator can sort by eye if they want).
// ---------------------------------------------------------------------------

const MAX_VISIBLE_PER_STATUS = 5;

// Mirror JobsReview's STATUS_COLORS so a card moving from widget to
// /jobs review page never changes appearance. Centralizing here would
// force a tiny utility module for two consumers; the duplication is
// intentional until a third consumer materializes.
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
      {hrs}h {mins}m left
    </span>
  );
}

// ``Apply`` CTA — identical semantics to JobsReview's ``handleMarkAsApplied``:
// opens the job URL in a new tab (setTimeout defers the open so popup
// blockers don't suppress it) AND fires the ``POST /api/applications``
// mutation that atomically flips the Job to status='applied' and creates
// the Application(submitted) row. The mutation's ``onSuccess`` invalidates
// the ['jobs'] cache so this card disappears from the widget on the next
// paint and reappears in the ApplicationTracker page.
function handleApply(job, createApplication) {
  if (!job?.url) return;
  setTimeout(() => {
    window.open(job.url, '_blank', 'noopener,noreferrer');
  }, 0);
  createApplication.mutate({ jobId: job.id, notes: null });
}

function CompactJobRow({
  job,
  approve,
  reject,
  createApplication,
}) {
  // Per-section disable — match JobsReview.jsx: rely on the global
  // mutation's ``isPending`` flag (no per-row variables lookup).
  // ``useMutation.variables`` is not guaranteed to clear to undefined
  // on settle across react-query v5 patches, so a ``variables ===
  // job.id`` check can briefly show a stale "Applying…" badge on the
  // wrong row during the post-mutation refetch window. The global
  // check is the conservative choice and the operator only mutates
  // one row at a time in practice.
  const isApplyPending = createApplication.isPending;
  const isApprovePending = approve.isPending;
  const isRejectPending = reject.isPending;

  return (
    <div className="bg-white border border-gray-200 rounded-lg px-4 py-3 flex items-center gap-4 hover:shadow-sm transition-shadow">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5 flex-wrap">
          <span
            className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700'}`}
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
          <TimeRemaining deadline={job.review_deadline} />
        </div>
        <h4 className="text-sm font-semibold text-gray-900 truncate">
          {job.title}
          <span className="font-normal text-gray-500 ml-2">· {job.company_name}</span>
        </h4>
      </div>

      <div className="flex items-center gap-2 shrink-0">
        {job.status === 'in_review' && (
          <>
            <button
              onClick={() => approve.mutate(job.id)}
              disabled={isApprovePending || isRejectPending}
              className="px-3 py-1.5 bg-green-600 text-white text-xs font-medium rounded-md hover:bg-green-700 disabled:opacity-50 transition-colors"
            >
              {isApprovePending ? '…' : 'Approve'}
            </button>
            <button
              onClick={() => reject.mutate(job.id)}
              disabled={isApprovePending || isRejectPending}
              className="px-3 py-1.5 bg-red-100 text-red-700 text-xs font-medium rounded-md hover:bg-red-200 disabled:opacity-50 transition-colors"
            >
              {isRejectPending ? '…' : 'Reject'}
            </button>
          </>
        )}
        {job.status === 'approved' && (
          <button
            onClick={() => handleApply(job, createApplication)}
            disabled={isApplyPending}
            title="Opens the posting in a new tab and records this as a submitted application."
            className="px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-md hover:bg-indigo-700 disabled:opacity-50 transition-colors"
          >
            {isApplyPending ? 'Applying…' : 'Apply ↗'}
          </button>
        )}
      </div>
    </div>
  );
}

function SectionHeader({ label, count, totalCount, linkTo = '/jobs' }) {
  // ``totalCount`` is the server-side unpaginated count returned by the
  // ``?status=...&page_size=N`` route (NOT ``jobs.length`` which is
  // capped at page_size). ``count`` is what we render in this widget
  // (capped at MAX_VISIBLE_PER_STATUS). "+ N more" only shows when
  // there are more rows on the Review page than fit here.
  const hiddenCount = totalCount - count;
  return (
    <div className="flex items-center justify-between mb-2">
      <div className="flex items-center gap-2">
        <h3 className="text-xs font-bold uppercase tracking-wide text-gray-600">{label}</h3>
        <span className="text-xs bg-gray-200 text-gray-700 px-1.5 py-0.5 rounded-full font-medium">
          {count}
        </span>
      </div>
      {hiddenCount > 0 && (
        <a
          href={linkTo}
          className="text-xs text-indigo-500 hover:underline"
        >
          + {hiddenCount} more on Review page →
        </a>
      )}
    </div>
  );
}

function SkeletonRow() {
  return <div className="bg-white border border-gray-200 rounded-lg px-4 py-3 animate-pulse h-14" />;
}

export default function PendingReviewWidget() {
  // Two parallel queries — one per actionable status. ``staleTime: 30s``
  // + ``refetchInterval: 60s`` come from the hook; mutations invalidate
  // the ['jobs'] cache on success so an Approve click flips this row
  // over to the "apply" section on the next render without a refetch.
  const { data: inReviewData, isLoading: inReviewLoading } = useJobs({
    status: 'in_review',
    page_size: MAX_VISIBLE_PER_STATUS,
  });
  const { data: approvedData, isLoading: approvedLoading } = useJobs({
    status: 'approved',
    page_size: MAX_VISIBLE_PER_STATUS,
  });
  const approve = useApproveJob();
  const reject = useRejectJob();
  const createApplication = useCreateApplication();

  const inReviewJobs = inReviewData?.jobs ?? [];
  const approvedJobs = approvedData?.jobs ?? [];
  const inReviewTotal = inReviewData?.total ?? 0;
  const approvedTotal = approvedData?.total ?? 0;
  const bannerCount = inReviewTotal + approvedTotal;

  // Hide widget entirely when both lists are confirmed empty. While either
  // query is still loading we render a skeleton so the widget doesn't
  // flash on/off as the network settles on initial mount.
  const isLoading = inReviewLoading || approvedLoading;
  if (!isLoading && bannerCount === 0) return null;

  const widgetBorder = approvedTotal > 0
    ? 'border-indigo-200 bg-gradient-to-b from-indigo-50/40'
    : 'border-gray-200';

  return (
    <section
      aria-label="Pending job review"
      className={`mb-6 border ${widgetBorder} rounded-xl p-4`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-bold text-gray-900">Pending review</h2>
          {bannerCount > 0 && (
            <span className="text-xs bg-indigo-600 text-white px-2 py-0.5 rounded-full font-semibold">
              {bannerCount}
            </span>
          )}
        </div>
        <a href="/jobs" className="text-xs text-indigo-500 hover:underline">
          Open Review queue →
        </a>
      </div>

      {/* "To review" — in_review jobs that need an Approve/Reject decision. */}
      <div className="mb-4">
        <SectionHeader
          label="📋 To review"
          count={isLoading ? 0 : inReviewJobs.length}
          totalCount={inReviewTotal}
        />
        <div className="space-y-2">
          {isLoading && inReviewLoading && (
            <>
              <SkeletonRow />
              <SkeletonRow />
            </>
          )}
          {!inReviewLoading && inReviewJobs.length === 0 && (
            <p className="text-xs text-gray-400 px-1 py-1">No jobs waiting on a decision.</p>
          )}
          {!inReviewLoading &&
            inReviewJobs.map((job) => (
              <CompactJobRow
                key={job.id}
                job={job}
                approve={approve}
                reject={reject}
                createApplication={createApplication}
              />
            ))}
        </div>
      </div>

      {/* "Ready to apply" — approved jobs waiting for the manual-apply handoff. */}
      <div>
        <SectionHeader
          label="🚀 Ready to apply"
          count={isLoading ? 0 : approvedJobs.length}
          totalCount={approvedTotal}
        />
        <div className="space-y-2">
          {isLoading && approvedLoading && (
            <>
              <SkeletonRow />
              <SkeletonRow />
            </>
          )}
          {!approvedLoading && approvedJobs.length === 0 && (
            <p className="text-xs text-gray-400 px-1 py-1">
              Approve a job above and it will appear here ready for one-click apply.
            </p>
          )}
          {!approvedLoading &&
            approvedJobs.map((job) => (
              <CompactJobRow
                key={job.id}
                job={job}
                approve={approve}
                reject={reject}
                createApplication={createApplication}
              />
            ))}
        </div>
      </div>
    </section>
  );
}
