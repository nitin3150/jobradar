const STAGES = ['', 'pre-seed', 'seed', 'series-a', 'series-b', 'series-c'];
const STARTUP_SOURCES = ['', 'sec_edgar', 'yc', 'vc_a16z', 'vc_sequoia', 'crunchbase', 'twitter', 'hackernews', 'techcrunch', 'producthunt'];
const NGO_SOURCES = ['', 'idealist', 'unjobs', 'techjobsforgood', 'reliefweb'];
const STATUSES = ['', 'new', 'contacted', 'interviewing', 'pass'];

export default function FilterBar({ filters, setFilters, category }) {
  const SOURCES = category === 'ngo' ? NGO_SOURCES : STARTUP_SOURCES;
  const update = (key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value || undefined, page: 1 }));
  };

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 mb-4 flex flex-wrap gap-4 items-end">
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Stage</label>
        <select
          value={filters.stage || ''}
          onChange={(e) => update('stage', e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
        >
          <option value="">All Stages</option>
          {STAGES.filter(Boolean).map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Source</label>
        <select
          value={filters.source || ''}
          onChange={(e) => update('source', e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
        >
          <option value="">All Sources</option>
          {SOURCES.filter(Boolean).map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Status</label>
        <select
          value={filters.status || ''}
          onChange={(e) => update('status', e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
        >
          <option value="">All</option>
          {STATUSES.filter(Boolean).map((s) => (
            <option key={s} value={s}>{s.charAt(0).toUpperCase() + s.slice(1)}</option>
          ))}
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
        <label className="text-xs font-medium text-gray-500 uppercase">Role</label>
        <input
          type="text"
          placeholder="e.g. ML Engineer"
          value={filters.role_keyword || ''}
          onChange={(e) => update('role_keyword', e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-40"
        />
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500 uppercase">Days</label>
        <select
          value={filters.days_ago || ''}
          onChange={(e) => update('days_ago', Number(e.target.value) || undefined)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white"
        >
          <option value="">All time</option>
          <option value="1">Today</option>
          <option value="7">Last 7 days</option>
          <option value="30">Last 30 days</option>
          <option value="90">Last 90 days</option>
        </select>
      </div>
    </div>
  );
}
