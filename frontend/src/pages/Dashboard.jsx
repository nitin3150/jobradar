import { useState } from 'react';
import { useCompanies, useCompanyStats } from '../hooks/useCompanies';
import { useCategory } from '../contexts/CategoryContext';
import StatusTracker from '../components/StatusTracker';
import FilterBar from '../components/FilterBar';
import CompanyFeed from '../components/CompanyFeed';
import OutreachPanel from '../components/OutreachPanel';

export default function Dashboard() {
  // Category is owned by <CategoryProvider>; re-mounting contents on change
  // naturally resets local pagination/selection state without a side-effecting
  // setState (and keeps the navbar chip + data filter in sync).
  const { category } = useCategory();
  return <DashboardContents key={category} category={category} />;
}

function DashboardContents({ category }) {
  const [filters, setFilters] = useState({ page: 1, page_size: 20 });
  const [selectedCompany, setSelectedCompany] = useState(null);

  const { data, isLoading } = useCompanies({ ...filters, category });
  const { data: stats } = useCompanyStats();

  const handlePageChange = (newPage) => {
    setFilters((prev) => ({ ...prev, page: newPage }));
  };

  return (
    <div className="max-w-6xl mx-auto px-4 py-6">
      <StatusTracker stats={stats} />
      <FilterBar filters={filters} setFilters={setFilters} category={category} />

      <CompanyFeed
        companies={data?.companies}
        isLoading={isLoading}
        onGenerateOutreach={(company) => setSelectedCompany(company)}
      />

      {/* Pagination */}
      {data && data.total_pages > 1 && (
        <div className="flex items-center justify-center gap-2 mt-6">
          <button
            onClick={() => handlePageChange(data.page - 1)}
            disabled={data.page <= 1}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded-lg disabled:opacity-50 hover:bg-gray-50"
          >
            Previous
          </button>
          <span className="text-sm text-gray-600">
            Page {data.page} of {data.total_pages}
          </span>
          <button
            onClick={() => handlePageChange(data.page + 1)}
            disabled={data.page >= data.total_pages}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded-lg disabled:opacity-50 hover:bg-gray-50"
          >
            Next
          </button>
        </div>
      )}

      {/* Outreach Panel Overlay */}
      {selectedCompany && (
        <>
          <div
            className="fixed inset-0 bg-black/20 z-40"
            onClick={() => setSelectedCompany(null)}
          />
          <OutreachPanel
            company={selectedCompany}
            onClose={() => setSelectedCompany(null)}
          />
        </>
      )}
    </div>
  );
}
