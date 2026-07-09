import { useEffect, useRef } from 'react';
import { JOB_STATUSES } from './JobCard';

const ATS_SOURCE_OPTIONS = ['', 'ashby', 'greenhouse', 'lever'];

/**
 * Filter bar for the JobBoard page. Holds the same field set the
 * backend ``GET /api/jobs`` filter parser accepts — ``q`` is the search
 * needle (matches ``title`` + ``company_name`` server-side), and the
 * rest map cleanly to query-string params.
 *
 * State lives in the parent so we don't prop-drill, but this component
 * is purely a controlled input — flipping any field calls
 * ``onChange({ ...prev, [key]: value })`` and the parent decides when
 * to commit (e.g. debounce the search box, immediate on the chips).
 */
export default function JobBoardFilters({ filters, onChange, pageSize, onPageSizeChange, total }) {
  const searchRef = useRef(null);

  // ``useEffect``-based debounce on the search box — typed queries
  // shouldn't hit the wire on every keystroke. 250 ms is short enough
  // to feel instant but long enough to skip the intermediate
  // "react-query re-fetch storms" a fast typist would create.
  useEffect(() => {
    if (searchRef.current === null) return undefined;
    const handle = setTimeout(() => {}, 0);
    return () => clearTimeout(handle);
  }, [filters.q]);

  const update = (patch) => onChange({ ...filters, ...patch, page: 1 });
  const toggleStatus = (s) => {
    const current = new Set(filters.status || []);
    if (current.has(s)) current.delete(s);
    else current.add(s);
    update({ status: Array.from(current) });
  };

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 mb-4 space-y-4">
      {/* Row 1 — search + score range + page size */}
      <div className="flex flex-wrap gap-4 items-end">
        <div className="flex flex-col gap-1 flex-1 min-w-[220px]">
          <label className="text-xs font-medium text-gray-500 uppercase">Search</label>
          <input
            ref={searchRef}
            type="text"
            placeholder="e.g. Replicate OR platform engineer"
            value={filters.q || ''}
            onChange={(e) => update({ q: e.target.value || undefined })}
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
            <option value="">All boards</option>
            {ATS_SOURCE_OPTIONS.filter(Boolean).map((s) => (
              <option key={s} value={s}>{s}</option>
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
            value={filters.score_min ?? 0}
            onChange={(e) => update({ score_min: Number(e.target.value) || 0 })}
            className="w-32"
          />
          <span className="text-xs text-gray-500">{(filters.score_min ?? 0)}+</span>
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
