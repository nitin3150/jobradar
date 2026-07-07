import CompanyCard from './CompanyCard';

export default function CompanyFeed({ opportunities, isLoading, onGenerateOutreach }) {
  if (isLoading) {
    return (
      <div className="space-y-4">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="bg-white border border-gray-200 rounded-xl p-5 animate-pulse">
            <div className="h-5 bg-gray-200 rounded w-1/3 mb-3" />
            <div className="h-4 bg-gray-100 rounded w-1/2 mb-2" />
            <div className="h-3 bg-gray-100 rounded w-3/4" />
          </div>
        ))}
      </div>
    );
  }

  if (!opportunities?.length) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-12 text-center">
        <p className="text-gray-500 text-lg">No opportunities in this window</p>
        <p className="text-gray-400 text-sm mt-1">
          Try widening the filter or running the pipeline from the navbar.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {opportunities.map((opportunity) => (
        <CompanyCard
          key={opportunity.id}
          opportunity={opportunity}
          onGenerateOutreach={onGenerateOutreach}
        />
      ))}
    </div>
  );
}
