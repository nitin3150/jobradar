import { useMutation, useQuery } from '@tanstack/react-query';
import { triggerPipeline, fetchPipelineStatus } from '../api/client';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { usePendingCount } from '../hooks/useJobs';
import ProfileMenu from './ProfileMenu';

function NavLink({ path, label, showBadge, count = 0 }) {
  const location = useLocation();
  const isActive = location.pathname === path;

  return (
    <Link
      to={path}
      className={`relative px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
        isActive
          ? 'bg-indigo-50 text-indigo-600'
          : 'text-gray-600 hover:text-gray-900 hover:bg-gray-100'
      }`}
    >
      {label}
      {showBadge && count > 0 && (
        <span className="absolute -top-1 -right-1 w-4 h-4 bg-red-500 text-white text-xs rounded-full flex items-center justify-center">
          {count > 9 ? '9+' : count}
        </span>
      )}
    </Link>
  );
}

// Four scanner categories — one per backend domain. `key` is the value
// CategoryContext stores; `label` is the navbar text. Job-board scans
// get their own dedicated ``/jobs`` page via the ``NavLink`` below
// rather than a tab here, so the operator can manage the full review
// queue (status, score, board, dates) without going through the
// dashboard.
const TABS = [
  { key: 'funding', label: 'Funding News' },
  { key: 'ngos', label: 'NGO Jobs' },
  { key: 'remote', label: 'Remote' },
  { key: 'oss', label: 'Open Source' },
];

export default function Navbar({ category, onCategoryChange }) {
  const navigate = useNavigate();
  const location = useLocation();

  const { data: status } = useQuery({
    queryKey: ['pipelineStatus'],
    queryFn: fetchPipelineStatus,
    refetchInterval: 30000,
  });

  const runPipeline = useMutation({ mutationFn: triggerPipeline });
  const { data: countData } = usePendingCount();
  const pendingCount = countData?.count || 0;

  return (
    <nav className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between sticky top-0 z-50">
      <div className="flex items-center gap-3">
        <Link to="/" className="flex items-center gap-3 group">
          <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center group-hover:bg-indigo-700 transition-colors">
            <span className="text-white font-bold text-sm">FR</span>
          </div>
          <h1 className="text-xl font-bold text-gray-900">FundingRadar</h1>
        </Link>

        {/* Five Scanner category tabs — each maps to a backend domain. */}
        <div className="flex items-center bg-gray-100 rounded-lg p-1 ml-2" role="tablist" aria-label="Scanner category">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              role="tab"
              aria-selected={category === tab.key}
              onClick={() => {
                onCategoryChange(tab.key);
                if (location.pathname !== '/') navigate('/');
              }}
              className={`px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                category === tab.key
                  ? 'bg-white text-indigo-600 shadow-sm'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Auto-Apply Nav Links (Q&A Bank moved into ProfileMenu) */}
        <div className="flex items-center gap-1 ml-3">
          <NavLink path="/jobs" label="Job Board" showBadge count={pendingCount} />
          <NavLink path="/applications" label="Applications" />
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
          <span className="text-xs text-gray-500 hidden sm:block">
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
        <ProfileMenu />
      </div>
    </nav>
  );
}
