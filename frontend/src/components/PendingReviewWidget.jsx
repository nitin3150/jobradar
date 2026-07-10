import { useCreateApplication } from '../hooks/useApplications';
import { useJobStatus, useJobs } from '../hooks/useJobs';

// ---------------------------------------------------------------------------
// ``PendingReviewWidget`` — a compact job-lifecycle panel that surfaces the
// actionable subset of the ``jobs`` table directly on the Dashboard.
//
// After the single-threshold simplification (board-scan and the scoring
// service both write ``status='approved'`` directly when a job clears the
// operator's ``JOB_FIT_THRESHOLD``), the ``in_review`` intermediate no longer
// receives fresh scans — it is reserved for any operator-driven status flips
// of already-archived rows. The widget now mirrors the apply queue itself:
//
// * ``status == 'approved'``: AI-scored + threshold-passing winners, queued
//   for the auto-apply worker. The operator sees top-N here (matching the
//   Navbar badge) and can fire the manual Apply CTA as a "skip the worker,
//   submit now" override — same semantics as the merged JobBoard page.
//   A **Pause** button sits beside Apply so the operator can park a
//   row before the worker fires (relocation, equity, visa, comp floor)
//   without leaving the Dashboard.
//
// * ``status == 'paused'``: operator-vetoed rows from the same scoring
//   pool. A **Resume** button restores the row to ``approved`` so the
//   worker can pick it up again. Rendered as a quieter sub-list so the
//   approved queue stays the visual primary surface.
//
// The widget hides its entire body (NOT just the rows) when BOTH the
// approved queue and the paused sub-list are empty — a quiet Dashboard
// (no fresh scans / no approved / no paused) stays scanner-focused.
//
// Data sourcing: TWO ``useJobs`` queries (status='approved' and
// status='paused'). We do not filter server-side for ``score DESC`` here
// because the backend's default ordering (''deadline_asc'' +
// ``ai_fit_score DESC`` secondary) is good enough for top-N review, and
// the AI-fit score is surfaced inline per row so the operator can sort
// by eye if they want.
// ---------------------------------------------------------------------------

const MAX_VISIBLE_PER_STATUS = 5;

// Mirror JobCard's STATUS_COLORS so a card moving from widget to the
// /jobs page never changes appearance. Centralizing here would force a
// tiny utility module for two consumers; the duplication is intentional
// until a third consumer materializes.
const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-green-100 text-green-800',
  // slate/gray for paused — reads as "parked / inactive" without
  // clashing with the traffic-light palette. Same colour used in
  // JobCard.jsx + FilterBar.jsx so a paused row renders the same
  // pill regardless of where it appears.
  paused: 'bg-slate-100 text-slate-800',
  rejected: 'bg-red-100 text-red-800',
  applied: 'bg-blue-100 text-blue-800',
  flagged: 'bg-orange-100 text-orange-800',
};

function TimeRemaining({ deadline }) {
  // Deadline widget kept around for any future operator-inserted
  // ``in_review`` row that still carries a ``review_deadline`` from
  // the pre-simplification flow. Today it almost never renders —
  // the widget surface is ``approved`` rows now — but a one-off
  // operator reclassification (drop a row back to ``in_review``)
  // would activate the badge. Cheap to keep.
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

// ``Apply`` CTA — same semantics as JobBoard's ``handleMarkAsApplied``:
// opens the job URL in a new tab (setTimeout defers the open so popup
// blockers don't suppress it) AND fires the ``POST /api/applications``
// mutation that atomically flips the Job to status='applied' and
// creates the Application(submitted) row. The mutation's ``onSuccess``
// invalidates the ['jobs'] cache so this card disappears from the
// widget on the next paint and reappears in the ApplicationTracker
// page.
function handleApply(job, createApplication) {
  if (!job?.url) return;
  setTimeout(() => {
    window.open(job.url, '_blank', 'noopener,noreferrer');
  }, 0);
  createApplication.mutate({ jobId: job.id, notes: null });
}

function CompactJobRow({ job, createApplication, setStatus }) {
  // Per-section disable — match JobBoard.jsx: rely on the global
  // mutation's ``isPending`` flag (no per-row variables lookup).
  // ``useMutation.variables`` is not guaranteed to clear to
  // undefined on settle across react-query v5 patches, so a
  // ``variables === job.id`` check can briefly show a stale
  // "Applying…" badge on the wrong row during the post-mutation
  // refetch window. The global check is the conservative choice
  // and the operator only mutates one row at a time in practice.
  const isApplyPending = createApplication.isPending;
  const isSetStatusPending = setStatus.isPending;

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
        {/* Per-status affordances. The single-threshold
            simplification removed the human-review gate; approved
            jobs are queued for the auto-apply worker. Apply is the
            "skip the worker, submit now" override. Pause parks the
            row BEFORE the worker fires (relocation / equity / visa
            / comp floor) so the operator has a veto path. Other
            statuses get no actions here — a freshly-`rejected` or
            `flagged` row sits in the widget silently until the
            operator navigates to the Job Board link. */}
        {job.status === 'approved' && (
          <>
            <button
              onClick={() =>
                setStatus.mutate({
                  id: job.id,
                  status: 'paused',
                  source: 'user',
                  note: 'paused from PendingReviewWidget',
                })
              }
              disabled={isSetStatusPending}
              title="Park this job so the apply worker skips it. Use when relocation, equity, visa, or comp is a deal-breaker."
              aria-label={`Pause ${job.title} at ${job.company_name}`}
              className="px-3 py-1.5 bg-white text-slate-700 text-xs font-medium rounded-md border border-slate-300 hover:bg-slate-50 disabled:opacity-50 transition-colors"
            >
              {isSetStatusPending ? 'Pausing…' : 'Pause'}
            </button>
            <button
              onClick={() => handleApply(job, createApplication)}
              disabled={isApplyPending}
              title="Opens the posting in a new tab and records this as a submitted application."
              className="px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-md hover:bg-indigo-700 disabled:opacity-50 transition-colors"
            >
              {isApplyPending ? 'Applying…' : 'Apply ↗'}
            </button>
          </>
        )}
        {job.status === 'paused' && (
          <button
            onClick={() =>
              setStatus.mutate({
                id: job.id,
                status: 'approved',
                source: 'user',
                note: 'resumed from PendingReviewWidget',
              })
            }
            disabled={isSetStatusPending}
            title="Return this row to the auto-apply queue."
            aria-label={`Resume ${job.title} at ${job.company_name}`}
            className="px-3 py-1.5 bg-slate-700 text-white text-xs font-medium rounded-md hover:bg-slate-800 disabled:opacity-50 transition-colors"
          >
            {isSetStatusPending ? 'Resuming…' : 'Resume'}
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
          + {hiddenCount} more on Job Board →
        </a>
      )}
    </div>
  );
}

function SkeletonRow() {
  return <div className="bg-white border border-gray-200 rounded-lg px-4 py-3 animate-pulse h-14" />;
}

export default function PendingReviewWidget() {
  // Two queries — ``status='approved'`` rows ready for the auto-apply
  // worker AND ``status='paused'`` rows the operator vetoed. Both
  // share the React Query cache keyed by ``['jobs', filters]`` so
  // mutations invalidate them on success and a Pause click pops the
  // row out of the top half into the bottom half on the next render
  // without a refetch.
  const { data: approvedData, isLoading: approvedLoading } = useJobs({
    status: 'approved',
    page_size: MAX_VISIBLE_PER_STATUS,
  });
  const { data: pausedData, isLoading: pausedLoading } = useJobs({
    status: 'paused',
    page_size: MAX_VISIBLE_PER_STATUS,
  });
  const createApplication = useCreateApplication();
  // Single ``useJobStatus`` mutation, reused twice with different
  // ``status`` arguments (pause = set status='paused'; resume = set
  // status='approved'). Both go through the canonical PATCH
  // ``/api/jobs/{id}/status`` endpoint so the job_status_history
  // audit-trail row is written in the same transaction, same source
  // default ('user'), and same note shape. No third hook needed.
  //
  // ``onError`` surfaces a 4xx/5xx failure as a console warning
  // (the row stays in the same status — the operator can retry or
  // use the JobBoard dropdown to recover). Without an explicit
  // handler React Query logs the rejection silently and the
  // operator sees a stuck "Pausing…" button for the duration of
  // the in-flight window.
  const setStatus = useJobStatus({
    onError: (err, variables) => {
      // eslint-disable-next-line no-console
      console.warn(
        `Failed to set job ${variables?.id} → ${variables?.status}:`,
        err?.message || err,
      );
    },
  });

  const approvedJobs = approvedData?.jobs ?? [];
  const approvedTotal = approvedData?.total ?? 0;
  const pausedJobs = pausedData?.jobs ?? [];
  const pausedTotal = pausedData?.total ?? 0;
  const bannerCount = approvedTotal;
  const pausedVisible = pausedTotal > 0;

  // Hide widget entirely when BOTH the approved queue AND the
  // paused sub-list are confirmed empty. While either query is
  // still loading we render a skeleton so the widget doesn't flash
  // on/off as the network settles on initial mount.
  if (!approvedLoading && !pausedLoading && bannerCount === 0 && !pausedVisible) return null;

  return (
    <section
      aria-label="Auto-apply queue"
      className="mb-6 border border-indigo-200 bg-gradient-to-b from-indigo-50/40 rounded-xl p-4"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-bold text-gray-900">Auto-apply queue</h2>
          {bannerCount > 0 && (
            <span className="text-xs bg-indigo-600 text-white px-2 py-0.5 rounded-full font-semibold">
              {bannerCount}
            </span>
          )}
          {pausedVisible && (
            <span className="text-xs bg-slate-200 text-slate-700 px-2 py-0.5 rounded-full font-medium">
              {pausedTotal} paused
            </span>
          )}
        </div>
        <a href="/jobs" className="text-xs text-indigo-500 hover:underline">
          Open Job Board →
        </a>
      </div>

      {/* Auto-apply queue — approved jobs waiting for (or already
          being processed by) the playwright apply worker. Manual
          Apply button here is a "skip the worker" override. Pause
          button here is the operator veto path so a row with a
          deal-breaker doesn't get auto-submitted. */}
      <div>
        <SectionHeader
          label="🚀 Approved · queued for auto-apply"
          count={approvedLoading ? 0 : approvedJobs.length}
          totalCount={approvedTotal}
        />
        <div className="space-y-2">
          {approvedLoading && (
            <>
              <SkeletonRow />
              <SkeletonRow />
            </>
          )}
          {!approvedLoading && approvedJobs.length === 0 && (
            <p className="text-xs text-gray-400 px-1 py-1">
              No approved jobs in the queue. The next boards scan will land more rows.
            </p>
          )}
          {!approvedLoading &&
            approvedJobs.map((job) => (
              <CompactJobRow
                key={job.id}
                job={job}
                createApplication={createApplication}
                setStatus={setStatus}
              />
            ))}
        </div>
      </div>

      {/* Paused sub-list — operator-vetoed rows from the same
          scoring pool. Resume button restores a row to ``approved``
          so the worker can pick it up again. Only renders when the
          paused pool is non-empty so the widget doesn't gain a
          useless second section when nothing is parked.

          Rendered AFTER (not alongside) the approved queue so the
          approved queue stays the visual primary surface; the
          pause action is the exception path. */}
      {pausedVisible && (
        <div className="mt-4 pt-4 border-t border-slate-200">
          <SectionHeader
            label="⏸ Paused · waiting for your call"
            count={pausedLoading ? 0 : pausedJobs.length}
            totalCount={pausedTotal}
          />
          <div className="space-y-2">
            {pausedLoading && <SkeletonRow />}
            {!pausedLoading &&
              pausedJobs.map((job) => (
                <CompactJobRow
                  key={job.id}
                  job={job}
                  createApplication={createApplication}
                  setStatus={setStatus}
                />
              ))}
          </div>
        </div>
      )}
    </section>
  );
}
