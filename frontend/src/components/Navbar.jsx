import { useMutation, useQuery } from '@tanstack/react-query';
import { triggerPipeline, fetchPipelineStatus } from '../api/client';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useApprovedCount } from '../hooks/useJobs';
import ProfileMenu from './ProfileMenu';

// Tab styling shared by the scanner-category buttons and the
// auto-apply nav links. Centralised so the merged navbar renders as
// ONE visual group with the same active/hover treatment for both
// ``<button>`` (scanner tabs) and ``<Link>`` (Job Board /
// Applications) children.
const TAB_BASE = 'px-3 py-1.5 text-sm font-medium rounded-md transition-colors';
const TAB_ACTIVE = 'bg-white text-indigo-600 shadow-sm';
const TAB_INACTIVE = 'text-gray-600 hover:text-gray-900';

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

// Auto-apply links live in the SAME tab group as the scanner
// categories (per v0.5 design: one unified tab strip). ``pending`` on
// ``/jobs`` powers the small red badge — the operator wants the
// queue size at-a-glance without scanning the page.
const NAV_LINKS = [
  { path: '/jobs', label: 'Job Board', showBadge: true },
  { path: '/applications', label: 'Applications', showBadge: false },
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
  // ``useApprovedCount`` returns the auto-apply queue size — the
  // number of ``status == "approved"`` rows the future apply
  // worker has to chew through. Renamed from ``usePendingCount``
  // after the v0.6 scoring flip because the old semantics (the
  // operator-review queue) no longer exists; the visual badge
  // stays on the ``/jobs`` NavLink so the operator can see queue
  // size without scanning the page.
  const { data: countData } = useApprovedCount();
  const approvedCount = countData?.count || 0;

  return (
    <nav className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between sticky top-0 z-50">
      <div className="flex items-center gap-3">
        <Link to="/" className="flex items-center gap-3 group">
          <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center group-hover:bg-indigo-700 transition-colors">
            <span className="text-white font-bold text-sm">FR</span>
          </div>
          <h1 className="text-xl font-bold text-gray-900">FundingRadar</h1>
        </Link>

        {/* Single unified tab group. v0.5 merge: scanner category
            buttons (state) + auto-apply nav links (router) share
            ONE outer ``bg-gray-100 rounded-lg p-1`` container so
            the strip reads as one cohesive UI with the same
            background (per the operator's "merge into one section"
            ask). Inside the container we keep the semantic split
            — the scanner tabs live in their own ``role="tablist"``
            and the auto-apply links live in a ``<nav>`` — so the
            visual is unified while the accessibility is correct.
            A 1px divider between the two groups sits *inside* the
            outer pill so the eye reads the strip as one continuous
            background with a subtle grouping, not two separate
            pills. */}
        <div className="flex items-center bg-gray-100 rounded-lg p-1 ml-2">
          <div
            className="flex items-center"
            role="tablist"
            aria-label="Scanner category"
          >
            {TABS.map((tab) => {
              const isActive = category === tab.key && location.pathname === '/';
              return (
                <button
                  key={tab.key}
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => {
                    onCategoryChange(tab.key);
                    if (location.pathname !== '/') navigate('/');
                  }}
                  className={`${TAB_BASE} ${isActive ? TAB_ACTIVE : TAB_INACTIVE}`}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>

          {/* 1px rail between the two groups — INSIDE the outer
              pill so the strip is one continuous background. ``h-4``
              (not h-5/h-6) keeps it from intersecting the pill's
              ``p-1`` top/bottom padding. */}
          <div className="w-px h-4 bg-gray-300 mx-1" aria-hidden="true" />

          <nav
            className="flex items-center"
            aria-label="Auto-apply"
          >
            {NAV_LINKS.map((link) => {
              const isActive = location.pathname === link.path;
              return (
                <Link
                  key={link.path}
                  to={link.path}
                  aria-current={isActive ? 'page' : undefined}
                  className={`${TAB_BASE} ${isActive ? TAB_ACTIVE : TAB_INACTIVE} ${
                    link.showBadge ? 'relative' : ''
                  }`}
                >
                  {link.label}
                  {link.showBadge && approvedCount > 0 && (
                    <span
                      aria-label={`${approvedCount} jobs queued for auto-apply`}
                      className="absolute -top-1 -right-1 w-4 h-4 bg-red-500 text-white text-xs rounded-full flex items-center justify-center"
                    >
                      {approvedCount > 9 ? '9+' : approvedCount}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>
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
