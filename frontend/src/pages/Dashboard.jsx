import { useMemo, useState } from 'react';
import { useCategory } from '../contexts/CategoryContext';
import { useScannerOpportunities } from '../hooks/useScanner';
import { useBoardOpportunities } from '../hooks/useBoardOpportunities';
import FilterBar from '../components/FilterBar';
import CompanyFeed from '../components/CompanyFeed';
import OutreachPanel from '../components/OutreachPanel';
import PendingReviewWidget from '../components/PendingReviewWidget';

const CATEGORY_INTROS = {
  funding: 'Startups and products making headlines in the last 24 hours, with founder / contact links for outreach.',
  ngos: 'Job openings posted by NGOs in the last 3 days — humanitarian, policy, advocacy roles.',
  remote: 'Remote-friendly roles updated across HN, Remotive, and RemoteOK in the last 24 hours.',
  boards: 'Engineering roles pulled from Ashby, Greenhouse, and Lever boards in the last hour.',
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

  // The boards tab reads from Supabase (`/api/jobs`); the other 4 tabs
  // (funding / ngos / remote / oss) keep live-scraping via
  // ``useScannerOpportunities``. Passing ``null`` as the category to
  // ``useScannerOpportunities`` disables its auto-trigger
  // (``enabled: !!category`` in the hook), so the unused branch costs
  // nothing — React Query just sits idle waiting for a real category.
  // ``useBoardOpportunities`` is unconditional because React's rules
  // of hooks forbid branching at the hook call site.
  const scannerQuery = useScannerOpportunities(
    category === 'boards' ? null : category,
    queryParams,
  );
  const boardQuery = useBoardOpportunities();
  const { data, isLoading } = category === 'boards' ? boardQuery : scannerQuery;

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

      {/* In-flow job-lifecycle widget — sits above the scanner feed so
          the operator's "Approve → Apply" hot path is the first thing
          they see on every Dashboard visit. Hides itself when there are
          no jobs awaiting a decision so the scanner view stays clean. */}
      <PendingReviewWidget />

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
