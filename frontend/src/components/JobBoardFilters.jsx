import { useEffect, useRef, useState } from 'react';
import { JOB_STATUSES } from './JobCard';

// Full set of ATS boards the backend accepts; the dropdown shows
// every value so the operator can drill into e.g. Greenhouse-only
// reviews. Mirrors ``ATS_BOARD_VALUES`` in :mod:`backend.db.models`.
const ATS_SOURCE_OPTIONS = [
  { value: '', label: 'All boards' },
  { value: 'ashby', label: 'Ashby' },
  { value: 'greenhouse', label: 'Greenhouse' },
  { value: 'lever', label: 'Lever' },
  { value: 'remotive', label: 'Remotive' },
  { value: 'remoteok', label: 'RemoteOK' },
  { value: 'hackernews', label: 'Hacker News' },
];

// Allowed ``?sort=`` values. The backend defaults to ``deadline_asc``
// when the param is missing or unknown, so the React UI's
// placeholder label is "Best match (deadline)" — the same behaviour
// the v0.4 page shipped with.
const SORT_OPTIONS = [
  { value: 'deadline_asc', label: 'Best match (deadline)' },
  { value: 'score_desc', label: 'Score: high to low' },
  { value: 'score_asc', label: 'Score: low to high' },
  { value: 'posted_desc', label: 'Newest posted' },
  { value: 'posted_asc', label: 'Oldest posted' },
];

/**
 * Filter bar for the JobBoard page. Holds the same field set the
 * backend ``GET /api/jobs`` filter parser accepts — ``q`` is the search
 * needle (matches ``title`` + ``company_name`` server-side), ``sort``
 * drives the new sort dropdown, and the rest map cleanly to
 * query-string params.
 *
 * v0.5 fixes from the v0.4 review:
 *  - **Search debounce**: the input is now backed by a shadow
 *    ``localQ`` state that syncs to the parent on a 250 ms debounce
 *    so a fast typist no longer floods ``useJobs`` with intermediate
 *    refetches.
 *  - **Score scale**: the min-score slider is 0-100% in the UI, but
 *    the backend expects 0.0-1.0. We divide by 100 just-in-time on
 *    the ``onChange`` path so the wire value matches the API.
 *  - **Sort**: new ``<select>`` driving the new ``?sort=`` query
 *    param. Defaults to ``deadline_asc`` to preserve v0.4 behaviour.
 *
 * State lives in the parent so we don't prop-drill, but this component
 * is purely a controlled input — flipping any field calls
 * ``onChange({ ...prev, [key]: value })`` and the parent decides when
 * to commit (e.g. debounce the search box, immediate on the chips).
 */
export default function JobBoardFilters({ filters, onChange, pageSize, onPageSizeChange, total }) {
  // ---- Search debounce (shadow local state) ----
  // The ``filters.q`` value (the one that hits the wire) is the
  // *debounced* value. We mirror the input field onto ``localQ`` so
  // every keystroke is responsive visually, and only call
  // ``onChange`` after 250 ms of idle. The trailing-edge debounce
  // is the standard pattern for "search-as-you-type" — a leading-
  // edge debounce would skip the first keystroke and feel laggy.
  const [localQ, setLocalQ] = useState(filters.q || '');
  // Mirror the latest ``filters`` into a ref so the debounce timer
  // callback always spreads the freshest filter state, not a stale
  // closure. Without this, a user who types in the search box and
  // then quickly changes another filter (board, status, dates)
  // before the 250 ms debounce fires would have the OTHER filter
  // changes overwritten when the timer commits — the operator
  // reported "search/status/board filters don't work", and this
  // stale-closure race was the root cause.
  const filtersRef = useRef(filters);
  useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);

  useEffect(() => {
    // External reset (e.g. parent "Reset filters" button) should
    // clear the local input too, otherwise the field drifts out of
    // sync with the actual query.
    setLocalQ(filters.q || '');
  }, [filters.q]);

  useEffect(() => {
    // Skip the initial mount synchronisation — the parent already
    // has whatever value it had; we only want to fire on changes.
    // The guard below also breaks the feedback loop: when our own
    // onChange commits, ``filters.q`` becomes ``localQ`` and the
    // reset effect mirrors it back, so without this guard the
    // debounce would re-emit on the next render.
    if (localQ === (filters.q || '')) return undefined;
    const handle = setTimeout(() => {
      // Read the freshest ``filters`` from the ref, NOT the
      // closure's ``filters`` — the closure is only refreshed on
      // localQ change, so any other filter change since the last
      // keystroke would be lost otherwise.
      const fresh = filtersRef.current;
      onChange({ ...fresh, q: localQ || undefined, page: 1 });
    }, 250);
    return () => clearTimeout(handle);
    // ``filters`` is intentionally omitted from deps: depending on it
    // would cause a feedback loop (the localQ → onChange → filters
    // update → effect re-runs → ...). We only want to react to
    // localQ changes; the next render after onChange commits is
    // already covered by the next localQ-keystroke cycle. The
    // ref-read above gives the timer access to fresh state without
    // a re-run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localQ]);

  const update = (patch) => onChange({ ...filters, ...patch, page: 1 });
  const toggleStatus = (s) => {
    const current = new Set(filters.status || []);
    if (current.has(s)) current.delete(s);
    else current.add(s);
    update({ status: Array.from(current) });
  };

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 mb-4 space-y-4">
      {/* Row 1 — search + score range + sort + page size */}
      <div className="flex flex-wrap gap-4 items-end">
        <div className="flex flex-col gap-1 flex-1 min-w-[220px]">
          <label className="text-xs font-medium text-gray-500 uppercase">Search</label>
          <input
            type="text"
            placeholder="e.g. Replicate OR platform engineer"
            value={localQ}
            onChange={(e) => setLocalQ(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white w-full"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs font-medium text-gray-500 uppercase">Board</label>
          <select
            value={filters.ats_type || ''}
            onChange={(e) => update({ ats_type: e.target.value || undefined })}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
          >
            {ATS_SOURCE_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs font-medium text-gray-500 uppercase" htmlFor="jobboard-sort">
            Sort
          </label>
          <select
            id="jobboard-sort"
            value={filters.sort || 'deadline_asc'}
            onChange={(e) => update({ sort: e.target.value })}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
          >
            {SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs font-medium text-gray-500 uppercase">Min score</label>
          <input
            type="range"
            min="0"
            max="100"
            step="5"
            // The slider's min/max are 0-100 (percent) but the
            // wire value is 0.0-1.0 (Pydantic Literal[0.0, 1.0] on
            // the backend). The previous version passed
            // ``filters.score_min ?? 0`` (a float in [0, 1]) as
            // the slider's ``value`` — the browser then positioned
            // the thumb at position N/100 of the range, so a 60%
            // state (0.6) put the thumb at position 0.6 and the
            // thumb visibly snapped back to the left after every
            // drag. Multiply by 100 so the thumb position matches
            // the percentage the operator sees in the label below.
            // ``step={5}`` constrains the user-drag output to
            // multiples of 5, so the round-trip
            // (state→slider→state) is always stable.
            value={Math.round((filters.score_min ?? 0) * 100)}
            onChange={(e) => update({ score_min: Number(e.target.value) / 100 })}
            className="w-32"
          />
          <span className="text-xs text-gray-500">{Math.round((filters.score_min ?? 0) * 100)}%+</span>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs font-medium text-gray-500 uppercase">Page size</label>
          <select
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
          >
            {[10, 20, 50, 100].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Row 2 — multi-status chips */}
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-xs font-medium text-gray-500 uppercase mr-1">Status</span>
        {JOB_STATUSES.map((s) => {
          const active = (filters.status || []).includes(s);
          return (
            <button
              key={s}
              type="button"
              onClick={() => toggleStatus(s)}
              className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${
                active
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'bg-white text-gray-600 border-gray-300 hover:border-indigo-400'
              }`}
            >
              {s.replace('_', ' ')}
            </button>
          );
        })}
        {(filters.status || []).length > 0 && (
          <button
            type="button"
            onClick={() => update({ status: undefined })}
            className="px-2 py-1 rounded text-xs text-gray-500 hover:text-gray-700 ml-2"
          >
            Clear status
          </button>
        )}
      </div>

      {/* Row 3 — date range */}
      <div className="flex flex-wrap gap-4 items-end">
        <DateField
          label="Posted from"
          value={filters.posted_from}
          onChange={(v) => update({ posted_from: v || undefined })}
        />
        <DateField
          label="Posted to"
          value={filters.posted_to}
          onChange={(v) => update({ posted_to: v || undefined })}
        />
        <div className="text-xs text-gray-500 ml-auto self-center">
          {typeof total === 'number' && (
            <span>
              <span className="font-semibold text-gray-900">{total}</span> matching job{total === 1 ? '' : 's'}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function DateField({ label, value, onChange }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs font-medium text-gray-500 uppercase">{label}</label>
      <input
        type="date"
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
      />
    </div>
  );
}
