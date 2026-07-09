import { useState } from 'react';
import { Link } from 'react-router-dom';

// Single status-pill colour map. Mirrors the legacy JobsReview styling
// so the merged page keeps the colour-by-status instant-readability the
// operator trained on — a green pill is approved, red is rejected,
// blue is applied, etc. Tests in components/__tests__ assert on these.
const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800 border-yellow-200',
  approved: 'bg-green-100 text-green-800 border-green-200',
  rejected: 'bg-red-100 text-red-800 border-red-200',
  applied: 'bg-blue-100 text-blue-800 border-blue-200',
  flagged: 'bg-orange-100 text-orange-800 border-orange-200',
};

// Both the ats_type badge colour and the source-board dropdown options
// share this list — adding a board means editing both the dropdown and
// the colour map. Kept in one place to avoid drift.
const ATS_COLORS = {
  ashby: 'bg-emerald-100 text-emerald-700',
  greenhouse: 'bg-green-100 text-green-700',
  lever: 'bg-yellow-100 text-yellow-700',
  remotive: 'bg-blue-100 text-blue-700',
  remoteok: 'bg-indigo-100 text-indigo-700',
  hackernews: 'bg-orange-100 text-orange-700',
};

// Five valid statuses — ``JobStatus`` Literal in the backend. Hard-
// coded here to avoid a roundtrip on first render. Kept in sync
// intentionally — the JobBoardStatusDropdown is the only writer and
// validates against this list before POSTing.
const JOB_STATUSES = ['in_review', 'approved', 'rejected', 'applied', 'flagged'];

const ATS_SOURCE_OPTIONS = ['', 'ashby', 'greenhouse', 'lever'];  // most common; backend returns the full set

function fmtDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtRelative(iso) {
  if (!iso) return null;
  const diff = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(diff)) return null;
  const days = Math.floor(diff / 86_400_000);
  if (days < 0) return 'just now';
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

/**
 * The JobBoard card. Each card carries:
 *
 * - Title + company name + ats_type badge
 * - AI-fit score (rendered 0..100)
 * - Status DROPDOWN (5-way) — wires directly to ``useJobStatus`` so a
 *   drop-down click writes a job_status_history row on the server.
 * - Posted date + Last updated date when the board gave us one
 * - Direct link to the actual posting
 * - "Interview Prep" button (always visible) — opens the modal
 * - "Generate outreach" button when the card is in any pre-apply state
 *
 * The card is intentionally free of any pagination / filter concerns
 * — those live one layer up in :class:`JobBoard` so a card is a
 * pure render of job fields. ``onInterviewPrep(id, job)`` and
 * ``onGenerateOutreach(job)`` hoist the action out so the page
 * controls modal open state.
 */
export default function JobCard({ job, onInterviewPrep, onChangeStatus, onGenerateOutreach, onMarkApplied }) {
  const [statusOpen, setStatusOpen] = useState(false);
  const postedRel = fmtRelative(job.posted_at);
  const postedAbs = fmtDate(job.posted_at);
  const updatedRel = fmtRelative(job.source_updated_at);
  const updatedAbs = fmtDate(job.source_updated_at);
  const scorePercent = job.ai_fit_score != null ? Math.round(job.ai_fit_score * 100) : null;
  const ats = (job.ats_type || '').toLowerCase();

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <Link
              to={`/jobs/${job.id}`}
              className="text-lg font-semibold text-gray-900 hover:text-indigo-600 transition-colors truncate max-w-[28rem]"
            >
              {job.title || '(untitled)'}
            </Link>
            <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
              ATS_COLORS[ats] || 'bg-gray-100 text-gray-700'
            }`}>
              {(job.ats_type || 'board').toUpperCase()}
            </span>
            {scorePercent != null && (
              <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${
                scorePercent >= 70
                  ? 'bg-green-50 text-green-700 border-green-200'
                  : scorePercent >= 40
                  ? 'bg-yellow-50 text-yellow-700 border-yellow-200'
                  : 'bg-red-50 text-red-700 border-red-200'
              }`}>
                {scorePercent}% fit
              </span>
            )}
          </div>
          <p className="text-sm font-medium text-gray-700 truncate">{job.company_name || '(unknown)'}</p>
        </div>

        {/* Status dropdown — click the badge to expand a 5-way menu.
            Submission happens on selection; calling ``onStatusChange``
            (passed by the parent page) threads through useJobStatus. */}
        <div className="relative shrink-0">
          <button
            type="button"
            onClick={() => setStatusOpen((v) => !v)}
            aria-haspopup="listbox"
            aria-expanded={statusOpen}
            className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold border transition-colors ${
              STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700 border-gray-200'
            } hover:brightness-95`}
          >
            <span>{(job.status || 'unknown').replace('_', ' ')}</span>
            <svg viewBox="0 0 20 20" className="w-3 h-3" aria-hidden="true">
              <path fill="currentColor" d="M5.5 7.5L10 12l4.5-4.5z" />
            </svg>
          </button>
          {statusOpen && (
            <ul
              role="listbox"
              className="absolute right-0 mt-1 z-20 bg-white border border-gray-200 rounded-lg shadow-lg py-1 w-36"
              onMouseLeave={() => setStatusOpen(false)}
            >
              {JOB_STATUSES.map((s) => (
                <li key={s}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={job.status === s}
                    onClick={() => {
                      setStatusOpen(false);
                      if (s !== job.status && typeof onChangeStatus === 'function') {
                        // Hand the desired status to the parent page so
                        // it can call ``useJobStatus().mutate({ id, status: s })``
                        // without the card needing to import the hook.
                        onChangeStatus(job, s);
                      }
                    }}
                    className={`w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 flex items-center justify-between ${
                      job.status === s ? 'font-semibold' : ''
                    }`}
                  >
                    <span className={`inline-block w-2 h-2 rounded-full ${
                      STATUS_COLORS[s].split(' ').find((c) => c.startsWith('bg-')) || 'bg-gray-400'
                    }`} aria-hidden="true" />
                    <span className="flex-1 ml-2 text-gray-700">{s.replace('_', ' ')}</span>
                    {job.status === s && <span aria-hidden="true">✓</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {job.ai_fit_reasoning && (
        <p className="text-sm text-gray-600 mb-3 line-clamp-2">
          <span className="text-gray-400 mr-1">AI:</span>
          {job.ai_fit_reasoning}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 mb-3">
        {postedRel && (
          <span title={postedAbs ? `Posted ${postedAbs}` : ''}>
            <span className="font-medium text-gray-600">Posted</span>{' '}
            {postedRel}
          </span>
        )}
        {updatedRel && updatedRel !== postedRel && (
          <span title={updatedAbs ? `Updated ${updatedAbs}` : ''}>
            <span className="font-medium text-gray-600">Updated</span>{' '}
            {updatedRel}
          </span>
        )}
        <a
          href={job.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-indigo-500 hover:underline"
        >
          View posting →
        </a>
      </div>

      <div className="flex items-center justify-end gap-2 pt-3 border-t border-gray-100">
        {onInterviewPrep && (
          <button
            type="button"
            onClick={() => onInterviewPrep('research', job)}
            className="text-xs px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
          >
            Interview Prep
          </button>
        )}
        {job.status === 'approved' && onMarkApplied && (
          <button
            type="button"
            onClick={() => onMarkApplied(job)}
            className="text-xs px-3 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            Mark as applied
          </button>
        )}
        {onGenerateOutreach && (
          <button
            type="button"
            onClick={() => onGenerateOutreach(job)}
            className="text-xs px-3 py-1.5 text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
          >
            Generate Outreach
          </button>
        )}
      </div>
    </div>
  );
}

// Re-export so the JobBoard page can render a fallback list of
// options without having to import the constants separately.
export { JOB_STATUSES, ATS_SOURCE_OPTIONS };
