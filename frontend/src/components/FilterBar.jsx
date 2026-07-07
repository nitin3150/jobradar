// Per-category source lists. The first empty option means "All sources" so
// the dropdown maps cleanly onto the backend's optional `sources` filter.

const SOURCE_LISTS = {
  funding: ['', 'producthunt', 'startupsgallery'],
  ngos: ['', 'reliefweb', 'idealist'],
  remote: ['', 'hackernews', 'remotive', 'remoteok'],
  boards: ['', 'ashby', 'greenhouse', 'lever'],
  oss: ['', 'github'],
};

export default function FilterBar({ filters, setFilters, category }) {
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
