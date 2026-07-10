import { useState } from 'react';
import { Link } from 'react-router-dom';
import Modal from './Modal';

// Single status-pill colour map. Mirrors the legacy JobsReview styling
// so the merged page keeps the colour-by-status instant-readability the
// operator trained on — a green pill is approved, red is rejected,
// blue is applied, etc. Tests in components/__tests__ assert on these.
const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800 border-yellow-200',
  approved: 'bg-green-100 text-green-800 border-green-200',
  // slate/gray for paused — reads as "inactive / parked" without
  // clashing with the traffic-light palette. Same colour also
  // mirrors :data:`background-color` in the React PendingReviewWidget
  // so a paused row popped up there renders the same pill.
  paused: 'bg-slate-100 text-slate-800 border-slate-200',
  rejected: 'bg-red-100 text-red-800 border-red-200',
  applied: 'bg-blue-100 text-blue-800 border-blue-200',
  flagged: 'bg-orange-100 text-orange-800 border-orange-200',
};

// ATS board badge color map — keyed LOWERCASE so the lookup stays
// decoupled from the title-case display text in :func:`boardLabel`.
// Adding a board means editing both the map and the label function.
const ATS_COLORS = {
  ashby: 'bg-emerald-100 text-emerald-700',
  greenhouse: 'bg-green-100 text-green-700',
  lever: 'bg-yellow-100 text-yellow-700',
  remotive: 'bg-blue-100 text-blue-700',
  remoteok: 'bg-indigo-100 text-indigo-700',
  hackernews: 'bg-orange-100 text-orange-700',
};

// Title-case display label for the ATS board badge. v0.5 polish:
// uppercase ``ASHBY``/``GREENHOUSE``/``LEVER`` looked shouty in the
// v0.4 cards; title-case ``Ashby``/``Greenhouse``/``Lever`` reads as
// a normal product name. Returns ``null`` for missing values so the
// card can skip the badge entirely — the operator found the
// v0.4 ``BOARD`` fallback meaningless ("why is there a tag called
// 'boards'?"). Omitting the badge keeps the header clean when the
// board didn't tell us its source.
function boardLabel(atsType) {
  const t = (atsType || '').toLowerCase().trim();
  if (!t) return null;
  return t.charAt(0).toUpperCase() + t.slice(1);
}

// Five valid statuses — ``JobStatus`` Literal in the backend. Hard-
// coded here to avoid a roundtrip on first render. Kept in sync
// intentionally — the JobBoardStatusDropdown is the only writer and
// validates against this list before POSTing.
// Six valid statuses — ``JobStatus`` Literal in the backend (post-v0.6
// single-threshold + paused-state transition). Hard-coded here to
// avoid a roundtrip on first render. Kept in sync intentionally —
// the JobBoardStatusDropdown is the only writer and validates
// against this list before POSTing.
const JOB_STATUSES = ['in_review', 'approved', 'paused', 'rejected', 'applied', 'flagged'];

const ATS_SOURCE_OPTIONS = ['', 'ashby', 'greenhouse', 'lever'];  // most common; backend returns the full set

// Full date + time format. Used inline on the card (per the v0.5
// operator ask: "show the date and time it published/posted") and
// also in the JobDetail metadata grid. ``hour: 'numeric'`` +
// ``minute: '2-digit'`` yields "4:51 PM" in the operator's locale.
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

// Truncate a description to ``maxChars`` characters at the nearest
// word boundary, appending an ellipsis. Used by the JobCard to
// decide whether the body is long enough to warrant a "Read more"
// affordance — pure presentational, so kept inside the component
// module instead of utils.
function truncate(text, maxChars = 220) {
  if (!text) return { text: '', truncated: false };
  if (text.length <= maxChars) return { text, truncated: false };
  // Walk back to the last whitespace inside the budget so we don't
  // chop a word in half (e.g. "engineer" → "engi…").
  const slice = text.slice(0, maxChars);
  const lastSpace = slice.lastIndexOf(' ');
  const cut = lastSpace > 80 ? slice.slice(0, lastSpace) : slice;
  return { text: `${cut.trimEnd()}…`, truncated: true };
}

/**
 * The JobBoard card. Each card carries:
 *
 * - Title + company name + ATS board badge (title-case)
 * - AI-fit score (rendered 0..100)
 * - Status DROPDOWN (5-way) — wires directly to ``useJobStatus`` so a
 *   drop-down click writes a job_status_history row on the server.
 * - Description preview (truncated) + "Read more" button when the
 *   body is longer than ~220 chars; clicking opens a modal with the
 *   full description body. The v0.4 ``AI: <reasoning>`` body line is
 *   removed — that text moved to ``JobDetail`` so the card can show
 *   the actual posting instead.
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
  const [descOpen, setDescOpen] = useState(false);
  // Three timestamps the operator wants to see inline on every card
  // (per the v0.5 ask "show the date and time it published/posted
  // and if reposted then show the updated date and time and also
  // the evaluation time at which time the GHA evaluated it"):
  //  - ``posted_at``    — board-published creation time
  //  - ``source_updated_at`` — board-published update time (if reposted)
  //  - ``created_at``   — our DB row creation time = the moment the
  //                        GitHub Actions scorer first inserted it
  // ``fmtDateTime`` is used inline (the operator asked for the full
  // date AND time, not just a relative "3 days ago"); the
  // ``fmtRelative`` form is kept as a tooltip on hover so the
  // operator can still glance at "how long ago" without losing the
  // absolute value.
  const postedAbs = fmtDateTime(job.posted_at);
  const postedRel = fmtRelative(job.posted_at);
  const updatedAbs = fmtDateTime(job.source_updated_at);
  const updatedRel = fmtRelative(job.source_updated_at);
  const evaluatedAbs = fmtDateTime(job.created_at);
  const evaluatedRel = fmtRelative(job.created_at);
  // "Reposted" = the board gave us a source_updated_at AND it
  // differs from the posted_at. Comparing ISO strings is safe
  // because both come from the same source in the same format.
  const isReposted = Boolean(
    job.source_updated_at && job.posted_at &&
    new Date(job.source_updated_at).getTime() > new Date(job.posted_at).getTime()
  );
  const scorePercent = job.ai_fit_score != null ? Math.round(job.ai_fit_score * 100) : null;
  const ats = (job.ats_type || '').toLowerCase();
  const boardName = boardLabel(job.ats_type);
  const desc = truncate(job.description, 220);

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
            {/* Board badge: rendered only when the ATS type is
                known. v0.5 polish — the v0.4 ``BOARD`` fallback
                was meaningless to the operator, so we just skip
                the badge entirely when the board didn't tell us. */}
            {boardName && (
              <span
                className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                  ATS_COLORS[ats] || 'bg-gray-100 text-gray-700'
                }`}
                title={`Source: ${boardName}`}
              >
                {boardName}
              </span>
            )}
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

      {/* Description preview + "Read more" affordance. v0.5
          replaces the v0.4 ``AI: <reasoning>`` line with the actual
          board-published description (truncated to ~220 chars).
          Empty / null descriptions skip the block entirely so the
          card layout collapses cleanly when a board omits the
          field. The "Read more" button is only rendered when the
          truncation actually dropped characters, so a short
          description doesn't show a no-op button. */}
      {desc.text && (
        <div className="mb-3">
          <p className="text-sm text-gray-700 leading-relaxed whitespace-pre-line">
            {desc.text}
          </p>
          {desc.truncated && (
            <button
              type="button"
              onClick={() => setDescOpen(true)}
              className="mt-1 text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline transition-colors"
            >
              Read more →
            </button>
          )}
        </div>
      )}

      {/* Timestamp strip. v0.5: show the FULL date + time inline
          (per the operator's "date and time it published/posted"
          ask) with the relative form as a hover tooltip so the
          operator can still scan "how long ago" without losing the
          absolute value. Three labels, rendered in this order:
          Posted, Updated (only if reposted), Evaluated. The
          "Evaluated" timestamp is ``created_at`` — the moment our
          DB first saw the row, which is the same moment the GHA
          scorer inserted it. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 mb-3">
        {postedAbs && (
          <span title={postedRel ? `Posted ${postedRel}` : ''}>
            <span className="font-medium text-gray-600">Posted</span>{' '}
            <span className="tabular-nums">{postedAbs}</span>
          </span>
        )}
        {isReposted && updatedAbs && (
          <span title={updatedRel ? `Updated ${updatedRel}` : ''}>
            <span className="font-medium text-gray-600">Updated</span>{' '}
            <span className="tabular-nums">{updatedAbs}</span>
          </span>
        )}
        {evaluatedAbs && (
          <span title={evaluatedRel ? `Evaluated ${evaluatedRel}` : ''}>
            <span className="font-medium text-gray-600">Evaluated</span>{' '}
            <span className="tabular-nums">{evaluatedAbs}</span>
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

      {/* Description modal — opens via the "Read more" button. The
          modal reuses the shared ``Modal`` component so it inherits
          the backdrop / ESC / body-scroll-lock contract for free. */}
      <Modal
        open={descOpen}
        onClose={() => setDescOpen(false)}
        title={job.title || 'Job description'}
        description={`${job.company_name || ''}${boardName ? ` · ${boardName}` : ''}`}
        widthClass="max-w-2xl"
        footer={
          <div className="flex items-center justify-between">
            <a
              href={job.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-indigo-600 hover:underline"
            >
              Open original posting →
            </a>
            <button
              type="button"
              onClick={() => setDescOpen(false)}
              className="text-xs px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700"
            >
              Close
            </button>
          </div>
        }
      >
        {job.description ? (
          <div className="text-sm text-gray-700 leading-relaxed whitespace-pre-line">
            {job.description}
          </div>
        ) : (
          <p className="text-sm text-gray-500">No description was provided by the source board.</p>
        )}
      </Modal>
    </div>
  );
}

// Re-export so the JobBoard page can render a fallback list of
// options without having to import the constants separately.
export { JOB_STATUSES, ATS_SOURCE_OPTIONS };
