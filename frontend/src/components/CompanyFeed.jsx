import CompanyCard from './CompanyCard';

export default function CompanyFeed({ companies, isLoading, onGenerateOutreach }) {
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

  if (!companies?.length) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-12 text-center">
        <p className="text-gray-500 text-lg">No companies found</p>
        <p className="text-gray-400 text-sm mt-1">Try adjusting your filters or run the pipeline</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {companies.map((company) => (
        <CompanyCard
          key={company.id}
          company={company}
          onGenerateOutreach={onGenerateOutreach}
        />
      ))}
    </div>
  );
}
