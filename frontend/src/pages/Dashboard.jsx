import { useMemo, useState } from 'react';
import { useCategory } from '../contexts/CategoryContext';
import { useScannerOpportunities } from '../hooks/useScanner';
import FilterBar from '../components/FilterBar';
import CompanyFeed from '../components/CompanyFeed';
import OutreachPanel from '../components/OutreachPanel';
import PendingReviewWidget from '../components/PendingReviewWidget';

const CATEGORY_INTROS = {
  funding: 'Startups and products making headlines in the last 24 hours, with founder / contact links for outreach.',
  ngos: 'Job openings posted by NGOs in the last 3 days — humanitarian, policy, advocacy roles.',
  remote: 'Remote-friendly roles updated across HN, Remotive, and RemoteOK in the last 24 hours.',
  oss: 'Trending open source repos — pick one where you can ship a PR and pitch the maintainer.',
};

export default function Dashboard() {
  const { category } = useCategory();
  // Re-mount on category change so local filter / pagination state resets cleanly.
  return <DashboardContents key={category} category={category} />;
}

function DashboardContents({ category }) {
  const [filters, setFilters] = useState({});
  const [selectedOpportunity, setSelectedOpportunity] = useState(null);

  const queryParams = useMemo(() => {
    const out = { delta_hours: filters.delta_hours, limit: 50 };
    if (filters.min_score != null) out.limit = Math.max(out.limit, filters.min_score);
    return out;
  }, [filters.delta_hours, filters.min_score]);

  // The four remaining tabs (funding / ngos / remote / oss) all
  // keep live-scraping via ``useScannerOpportunities``. The boards
  // category was retired — its content lives on the new ``/jobs``
  // JobBoard page where it shares the canonical ``jobs`` table with
  // the manual-apply handoff. Passing the active category is
  // unconditional so the hook's ``enabled: !!category`` check fires.
  const scannerQuery = useScannerOpportunities(category, queryParams);
  const { data, isLoading } = scannerQuery;

  const opportunities = useMemo(() => {
    const list = data?.opportunities || [];
    if (!filters.q) return list;
    const needle = filters.q.toLowerCase();
    return list.filter((opp) =>
      (opp.title || '').toLowerCase().includes(needle) ||
      (opp.organization || '').toLowerCase().includes(needle) ||
      (opp.description || '').toLowerCase().includes(needle)
    );
  }, [data, filters.q]);

  return (
    <div className="max-w-6xl mx-auto px-4 py-6">
      <p className="text-sm text-gray-500 mb-4">{CATEGORY_INTROS[category]}</p>
      <div className="bg-white border border-gray-200 rounded-xl px-4 py-3 mb-4 text-sm text-gray-600 flex items-center justify-between">
        <span>
          <span className="font-semibold text-gray-900">{data?.count ?? 0}</span>{' '}
          {data?.opportunities ? 'matches' : 'opportunities'} in the last {data?.delta_hours ?? '—'}h
        </span>
        <span className="text-xs text-gray-400">{category.toUpperCase()} domain</span>
      </div>

      <FilterBar filters={filters} setFilters={setFilters} category={category} />

      <CompanyFeed
        opportunities={opportunities}
        isLoading={isLoading}
        onGenerateOutreach={(opp) => setSelectedOpportunity(opp)}
      />

      {selectedOpportunity && (
        <>
          <div
            className="fixed inset-0 bg-black/20 z-40"
            onClick={() => setSelectedOpportunity(null)}
          />
          <OutreachPanel
            company={selectedOpportunity}
            onClose={() => setSelectedOpportunity(null)}
          />
        </>
      )}
    </div>
  );
}
