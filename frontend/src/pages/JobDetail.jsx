import { useParams, Link } from 'react-router-dom';
import { useJob, useJobStatus } from '../hooks/useJobs';
import { useLatestResearch, useResearchMutation } from '../hooks/useResearch';
import { renderMarkdown } from '../utils/markdown';

const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  applied: 'bg-blue-100 text-blue-800',
  flagged: 'bg-orange-100 text-orange-800',
};

function fmtDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString();
}

// Full date + time, same shape the JobCard uses for the inline
// Posted/Updated/Evaluated strip. v0.5 polish: the operator asked
// for date AND time on every card; the detail page should match
// so the two surfaces read consistently.
function fmtDateTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

// The Research section renders the latest cached report (if one
// exists) inline and exposes a Generate / Regenerate button that
// fires ``useResearchMutation`` on click. Auto-firing on mount
// would burn 15-60s of LLM wall-clock + tokens for every deep-link
// visit; the operator opts in when they actually want the brief.
function ResearchSection({ jobId }) {
  const latest = useLatestResearch(jobId);
  const mutation = useResearchMutation();

  // ``latest`` is loading on first mount for a fresh jobId.
  if (latest.isLoading) {
    return (
      <div className="text-sm text-gray-500" data-testid="research-loading">
        Checking for cached research…
      </div>
    );
  }

  // 404 (no report yet) — show the Generate button. The
  // ``useLatestResearch`` hook returns ``isError: true`` when the
  // server returns 404 (axios error); the React Query retry is
  // off so this is a single, fast roundtrip.
  if (latest.isError) {
    return (
      <div className="space-y-3" data-testid="research-empty">
        <p className="text-sm text-gray-500">
          No research brief cached yet. Generate one below — typically
          10–60 seconds.
        </p>
        <button
          type="button"
          onClick={() => mutation.mutate(jobId)}
          disabled={mutation.isPending}
          className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
        >
          {mutation.isPending ? 'Generating…' : 'Generate Research Brief'}
        </button>
      </div>
    );
  }

  // Have a cached report — render the Markdown body and a
  // Regenerate button.
  const report = latest.data;
  return (
    <div className="space-y-3" data-testid="research-ready">
      {report?.generated_at && (
        <p className="text-xs text-gray-500">
          Generated {fmtDate(report.generated_at)}
          {report.model_used && (
            <>
              {' '}
              by <span className="font-mono">{report.model_used}</span>
            </>
          )}
        </p>
      )}
      {report?.content ? (
        <div data-testid="research-content">{renderMarkdown(report.content)}</div>
      ) : (
        <p className="text-sm text-gray-500">
          Cached report has no body — try Regenerate.
        </p>
      )}
      <button
        type="button"
        onClick={() => mutation.mutate(jobId)}
        disabled={mutation.isPending}
        className="text-xs px-3 py-1.5 text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
      >
        {mutation.isPending ? 'Regenerating…' : 'Regenerate'}
      </button>
    </div>
  );
}

/**
 * The single-job detail page. Drives the ``/jobs/:id`` route the
 * JobCard title-as-link points to and the Related-Jobs list on
 * ``CompanyDetail`` points to.
 *
 * Two top-level sections:
 *   1. Job posting — title, company, AI score, status pill,
 *      reasoning, source URL, posted/updated timestamps.
 *   2. Research — cached report (if any) + Generate / Regenerate
 *      button. LLM is opt-in; auto-firing on every deep-link
 *      visit would be too expensive.
 *
 * Status dropdown: the same ``useJobStatus`` mutation the JobCard
 * uses, so changing status here writes a ``job_status_history``
 * row in the same tx and invalidates the ``['jobs']`` cache (so
 * the JobBoard picks up the new state when the operator
 * navigates back).
 */
export default function JobDetail() {
  const { id } = useParams();
  const { data: job, isLoading, isError, error } = useJob(id);
  const jobStatus = useJobStatus();

  if (isLoading) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-4 bg-gray-100 rounded w-1/2" />
          <div className="h-40 bg-gray-100 rounded" />
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-8 text-center">
        <p className="text-gray-500">
          {error?.response?.status === 404
            ? `Job ${id} not found.`
            : 'Failed to load job — please retry.'}
        </p>
        <Link to="/jobs" className="text-indigo-600 hover:underline mt-2 inline-block">
          Back to Job Board
        </Link>
      </div>
    );
  }

  if (!job) return null;

  const scorePercent =
    job.ai_fit_score != null ? Math.round(job.ai_fit_score * 100) : null;

  const handleStatusChange = (next) => {
    if (next === job.status) return;
    jobStatus.mutate({ id: job.id, status: next, source: 'user' });
  };

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      {/* Back link */}
      <Link
        to="/jobs"
        className="text-sm text-indigo-600 hover:underline mb-4 inline-block"
      >
        &larr; Back to Job Board
      </Link>

      {/* Posting header */}
      <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="flex-1 min-w-0">
            <h1 className="text-2xl font-bold text-gray-900 break-words">
              {job.title}
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              {job.company_name} · {job.ats_type}
            </p>
          </div>
          <div className="shrink-0 flex flex-col items-end gap-2">
            <span
              className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold border ${
                STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700 border-gray-200'
              }`}
            >
              {job.status.replace('_', ' ')}
            </span>
            {scorePercent != null && (
              <span className="text-sm font-semibold text-indigo-600">
                {scorePercent}% fit
              </span>
            )}
          </div>
        </div>

        {job.ai_fit_reasoning && (
          <div className="mb-4">
            <p className="text-xs text-gray-500 uppercase mb-1">AI Reasoning</p>
            <p className="text-sm text-gray-700 whitespace-pre-wrap">
              {job.ai_fit_reasoning}
            </p>
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 text-sm">
          <div>
            <p className="text-xs text-gray-500 uppercase">Posted</p>
            <p className="font-medium">{fmtDateTime(job.posted_at) || '—'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 uppercase">Source updated</p>
            <p className="font-medium">{fmtDateTime(job.source_updated_at) || '—'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 uppercase">Evaluated</p>
            <p className="font-medium" title="When the GHA scorer first inserted this row">{fmtDateTime(job.created_at) || '—'}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 uppercase">Last touched</p>
            <p className="font-medium">{fmtDateTime(job.updated_at) || '—'}</p>
          </div>
        </div>

        <div className="flex items-center justify-between pt-3 border-t border-gray-100">
          <a
            href={job.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-indigo-600 hover:underline"
          >
            View posting →
          </a>
          <div className="flex items-center gap-2">
            <label htmlFor="job-detail-status" className="text-xs text-gray-500 uppercase">Status</label>
            <select
              id="job-detail-status"
              value={job.status}
              onChange={(e) => handleStatusChange(e.target.value)}
              disabled={jobStatus.isPending}
              className="text-xs border border-gray-300 rounded-lg px-2 py-1 bg-white"
            >
              {['in_review', 'approved', 'rejected', 'applied', 'flagged'].map((s) => (
                <option key={s} value={s}>
                  {s.replace('_', ' ')}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Description section — v0.5: the board-published posting
          body now lives on the ``jobs.description`` column and is
          rendered here in full so the operator can read the
          posting without leaving the app. ``whitespace-pre-line``
          preserves the line breaks a board's HTML often flattens
          to ``\n`` between paragraphs.

          Null fallback: pre-migration rows have ``description=NULL``
          (the v0.5 migration deliberately does not backfill). For
          those rows we still render the section with a subtle
          italicised placeholder so the layout doesn't jump
          unexpectedly when the operator navigates from a row that
          has a description to one that doesn't. */}
      <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
        <h2 className="text-sm font-semibold text-gray-900 mb-3">
          Job Description
        </h2>
        {job.description ? (
          <div className="text-sm text-gray-700 leading-relaxed whitespace-pre-line">
            {job.description}
          </div>
        ) : (
          <p className="text-sm text-gray-500 italic">
            No description was provided by the source board.
          </p>
        )}
      </div>

      {/* Research section */}
      <div className="bg-white border border-gray-200 rounded-xl p-6">
        <h2 className="text-sm font-semibold text-gray-900 mb-3">
          Interview Prep
        </h2>
        <ResearchSection jobId={job.id} />
      </div>
    </div>
  );
}
