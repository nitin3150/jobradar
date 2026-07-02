import { useMutation } from '@tanstack/react-query';
import { triggerPipeline, fetchPipelineStatus } from '../api/client';
import { useQuery } from '@tanstack/react-query';

export default function Navbar({ category, onCategoryChange }) {
  const { data: status } = useQuery({
    queryKey: ['pipelineStatus'],
    queryFn: fetchPipelineStatus,
    refetchInterval: 30000,
  });

  const runPipeline = useMutation({ mutationFn: triggerPipeline });

  const tabs = [
    { key: 'startup', label: 'Startups' },
    { key: 'ngo', label: 'NGO Jobs' },
  ];

  return (
    <nav className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between sticky top-0 z-50">
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center">
            <span className="text-white font-bold text-sm">FR</span>
          </div>
          <h1 className="text-xl font-bold text-gray-900">FundingRadar</h1>
        </div>

        {/* Category Tabs */}
        <div className="flex items-center bg-gray-100 rounded-lg p-1">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => onCategoryChange(tab.key)}
              className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
                category === tab.key
                  ? 'bg-white text-indigo-600 shadow-sm'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-4">
        {status?.is_running && (
          <span className="flex items-center gap-2 text-sm text-amber-600">
            <span className="w-2 h-2 bg-amber-500 rounded-full animate-pulse" />
            Pipeline running...
          </span>
        )}
        {status?.last_run_at && !status?.is_running && (
          <span className="text-xs text-gray-500">
            Last run: {new Date(status.last_run_at).toLocaleString()}
          </span>
        )}
        <button
          onClick={() => runPipeline.mutate()}
          disabled={runPipeline.isPending || status?.is_running}
          className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {runPipeline.isPending ? 'Running...' : 'Run Pipeline'}
        </button>
      </div>
    </nav>
  );
}
