// FilterBar — the per-page filter row.
//
// Two variants:
//   * ``"default"`` (CompanyFeed): category-specific source dropdown,
//     delta_hours window, single min_score slider, q keyword input.
//     Preserves the v0.4 wire shape (?source, ?delta_hours, ?min_score, ?q).
//   * ``"jobs"`` (JobsReview): keyword input, multi-status pill toggle,
//     ats_type dropdown, score range dual-handle slider (min + max),
//     posted_from / posted_to date inputs. Wires directly to the new
//     ``/api/jobs`` query params (?q, ?status multi, ?ats_type,
//     ?score_min / ?score_max, ?posted_from / ?posted_to).
//
// The dual-handle ``DualRangeSlider`` below is the single visual
// widget that emits both ``scoreMin`` and ``scoreMax`` (each held
// as a 0-100 integer in the parent; JobsReview divides by 100 when
// assembling the API params). The slider uses the standard
// overlapping-``input[type=range]`` pattern with a CSS-styled
// pointer-events: none track so clicks pass through to whichever
// thumb is on top; the complementary CSS lives in
// ``frontend/src/index.css`` under ``.dual-range-thumb``.
//
// Page is intentionally NOT a FilterBar concern — JobsReview holds
// it separately and resets to 1 on any filter change (handled by
// the page component, not the FilterBar).

const STATUSES = ['in_review', 'approved', 'rejected', 'flagged', 'applied'];
const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800 border-yellow-300',
  approved: 'bg-green-100 text-green-800 border-green-300',
  rejected: 'bg-red-100 text-red-800 border-red-300',
  applied: 'bg-blue-100 text-blue-800 border-blue-300',
  flagged: 'bg-orange-100 text-orange-800 border-orange-300',
};
const ATS_TYPES = ['', 'ashby', 'greenhouse', 'lever', 'remotive'];

// Per-category source lists. The first empty option means "All sources" so
// the dropdown maps cleanly onto the backend's optional `sources` filter.
const SOURCE_LISTS = {
  funding: ['', 'producthunt', 'startupsgallery'],
  ngos: ['', 'reliefweb', 'idealist'],
  remote: ['', 'hackernews', 'remotive', 'remoteok'],
  boards: ['', 'ashby', 'greenhouse', 'lever'],
  oss: ['', 'github'],
};

// Dual-handle score range slider. Two overlapping <input[type=range]>
// elements with a shared visual track + a colored range between the
// thumbs. The CSS for the transparent track + visible thumb lives in
// ``index.css`` so the markup here stays declarative.
//
// ``value`` is a 2-tuple [lo, hi] of integers in [min, max]. The
// onChange callback emits the same shape; callers clamp internally
// so this component always sees a non-crossing pair (lo <= hi).
function DualRangeSlider({ min, max, step = 1, value, onChange }) {
  const [lo, hi] = value;
  const loPct = ((lo - min) / (max - min)) * 100;
  const hiPct = ((hi - min) / (max - min)) * 100;
  return (
    <div className="relative h-6 w-44">
      {/* gray base track (pointer-events default; clicks pass through) */}
      <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 h-1.5 bg-gray-200 rounded-full pointer-events-none" />
      {/* colored range between the two thumbs */}
      <div
        className="absolute top-1/2 -translate-y-1/2 h-1.5 bg-indigo-500 rounded-full pointer-events-none"
        style={{ left: `${loPct}%`, right: `${100 - hiPct}%` }}
      />
      {/* lower-thumb input — clamps to the current hi value */}
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={lo}
        onChange={(e) => {
          const next = Math.min(Number(e.target.value), hi);
          onChange([next, hi]);
        }}
        aria-label="Minimum score"
        className="dual-range-thumb"
      />
      {/* upper-thumb input — clamps to the current lo value */}
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={hi}
        onChange={(e) => {
          const next = Math.max(Number(e.target.value), lo);
          onChange([lo, next]);
        }}
        aria-label="Maximum score"
        className="dual-range-thumb"
      />
    </div>
  );
}

export default function FilterBar({ filters, setFilters, category, variant = 'default' }) {
  // Jobs variant — full new filter row for /api/jobs query params.
  if (variant === 'jobs') {
    const selectedStatuses = filters.statuses || [];
    const scoreMin = filters.scoreMin ?? 0;
    const scoreMax = filters.scoreMax ?? 100;

    const update = (key, value) => {
      setFilters((prev) => ({ ...prev, [key]: value }));
    };
    const toggleStatus = (s) => {
      const next = selectedStatuses.includes(s)
        ? selectedStatuses.filter((x) => x !== s)
        : [...selectedStatuses, s];
      update('statuses', next);
    };

    return (
      <div className="bg-white border border-gray-200 rounded-xl p-4 mb-4 space-y-3">
        {/* Top row: search + ats_type + score range */}
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex flex-col gap-1">
            <label htmlFor="filter-q" className="text-xs font-medium text-gray-500 uppercase">Keyword</label>
            <input
              id="filter-q"
              type="text"
              placeholder="e.g. ML engineer"
              value={filters.q || ''}
              onChange={(e) => update('q', e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-40"
            />
          </div>

          <div className="flex flex-col gap-1">
            <label htmlFor="filter-ats-type" className="text-xs font-medium text-gray-500 uppercase">ATS</label>
            <select
              id="filter-ats-type"
              value={filters.ats_type || ''}
              onChange={(e) => update('ats_type', e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
            >
              <option value="">All ATS</option>
              {ATS_TYPES.filter(Boolean).map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-gray-500 uppercase">Score range</span>
            <DualRangeSlider
              min={0}
              max={100}
              step={5}
              value={[scoreMin, scoreMax]}
              onChange={([lo, hi]) => {
                update('scoreMin', lo);
                update('scoreMax', hi);
              }}
            />
            <span className="text-xs text-gray-500 tabular-nums">
              {scoreMin}% – {scoreMax}%
            </span>
          </div>
        </div>

        {/* Status multi-select — pill toggle. Zero active = "all statuses". */}
        <div className="flex flex-wrap gap-2 items-center">
          <span className="text-xs font-medium text-gray-500 uppercase mr-2">Status</span>
          {STATUSES.map((s) => {
            const active = selectedStatuses.includes(s);
            return (
              <button
                key={s}
                type="button"
                onClick={() => toggleStatus(s)}
                aria-pressed={active}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                  active
                    ? `${STATUS_COLORS[s]} border-current`
                    : 'bg-white text-gray-500 border-gray-300 hover:border-gray-400'
                }`}
              >
                {s.replace('_', ' ')}
              </button>
            );
          })}
          {selectedStatuses.length === 0 && (
            <span className="text-xs text-gray-400 italic">all statuses</span>
          )}
          {selectedStatuses.length > 0 && (
            <button
              type="button"
              onClick={() => update('statuses', [])}
              className="text-xs text-gray-400 hover:text-gray-600 underline ml-1"
            >
              clear
            </button>
          )}
        </div>

        {/* Posted date range */}
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex flex-col gap-1">
            <label htmlFor="filter-posted-from" className="text-xs font-medium text-gray-500 uppercase">Posted from</label>
            <input
              id="filter-posted-from"
              type="date"
              value={filters.postedFrom || ''}
              onChange={(e) => update('postedFrom', e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="filter-posted-to" className="text-xs font-medium text-gray-500 uppercase">Posted to</label>
            <input
              id="filter-posted-to"
              type="date"
              value={filters.postedTo || ''}
              onChange={(e) => update('postedTo', e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
            />
          </div>
        </div>
      </div>
    );
  }

  // Default variant — v0.4 CompanyFeed filter row (unchanged behavior).
  const sources = SOURCE_LISTS[category] || [''];
  const update = (key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value || undefined }));
  };

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 mb-4 flex flex-wrap gap-4 items-end">
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Source</label>
        <select
          value={filters.source || ''}
          onChange={(e) => update('source', e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
        >
          <option value="">All sources</option>
          {sources.filter(Boolean).map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Window</label>
        <select
          value={filters.delta_hours || ''}
          onChange={(e) => update('delta_hours', Number(e.target.value) || undefined)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
        >
          <option value="">Default</option>
          <option value="24">Last 24 hours</option>
          <option value="72">Last 3 days</option>
          <option value="168">Last week</option>
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Min Score</label>
        <input
          type="range"
          min="0"
          max="100"
          value={filters.min_score || 0}
          onChange={(e) => update('min_score', Number(e.target.value) || undefined)}
          className="w-32"
        />
        <span className="text-xs text-gray-500">{filters.min_score || 0}+</span>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Keyword</label>
        <input
          type="text"
          placeholder={
            category === 'oss' ? 'e.g. tokenizer' :
            category === 'ngos' ? 'e.g. policy' :
            'e.g. ML engineer'
          }
          value={filters.q || ''}
          onChange={(e) => update('q', e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-40"
        />
      </div>
    </div>
  );
}
