import { useState } from 'react';
import { useJobs, useApproveJob, useRejectJob } from '../hooks/useJobs';

const STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  applied: 'bg-blue-100 text-blue-800',
  flagged: 'bg-orange-100 text-orange-800',
};

function TimeRemaining({ deadline }) {
  if (!deadline) return null;
  const ms = new Date(deadline) - new Date();
  if (ms <= 0) return <span className="text-red-500 text-xs">Expired</span>;
  const hrs = Math.floor(ms / 3600000);
  const mins = Math.floor((ms % 3600000) / 60000);
  return (
    <span className="text-xs text-gray-500">
      {hrs}h {mins}m remaining
    </span>
  );
}

export default function JobsReview() {
  const [statusFilter, setStatusFilter] = useState('in_review');
  const { data, isLoading } = useJobs({ status: statusFilter, page_size: 50 });
  const approve = useApproveJob();
  const reject = useRejectJob();

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Jobs Review Queue</h1>
          <div className="flex gap-2">
            {['in_review', 'approved', 'rejected', 'flagged', 'applied'].map((s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                  statusFilter === s
                    ? 'bg-indigo-600 text-white border-indigo-600'
                    : 'bg-white text-gray-600 border-gray-300 hover:border-indigo-400'
                }`}
              >
                {s.replace('_', ' ')}
              </button>
            ))}
          </div>
        </div>

        {isLoading && (
          <div className="text-center py-12 text-gray-500">Loading jobs...</div>
        )}

        {!isLoading && data?.jobs?.length === 0 && (
          <div className="text-center py-12 text-gray-400">No jobs in this status.</div>
        )}

        <div className="space-y-4">
          {data?.jobs?.map((job) => (
            <div
              key={job.id}
              className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${
                        STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700'
                      }`}
                    >
                      {job.status.replace('_', ' ')}
                    </span>
                    <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                      {job.ats_type}
                    </span>
                    {job.ai_fit_score != null && (
                      <span className="text-xs font-medium text-indigo-600">
                        {Math.round(job.ai_fit_score * 100)}% fit
                      </span>
                    )}
                  </div>
                  <h3 className="text-lg font-semibold text-gray-900 truncate">
                    {job.title}
                  </h3>
                  <p className="text-sm text-gray-500 mt-0.5">{job.company_name}</p>
                  {job.ai_fit_reasoning && (
                    <p className="text-sm text-gray-600 mt-2 line-clamp-2">
                      {job.ai_fit_reasoning}
                    </p>
                  )}
                  <div className="flex items-center gap-4 mt-2">
                    <TimeRemaining deadline={job.review_deadline} />
                    <a
                      href={job.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-indigo-500 hover:underline"
                    >
                      View posting →
                    </a>
                  </div>
                </div>

                {job.status === 'in_review' && (
                  <div className="flex gap-2 ml-4 shrink-0">
                    <button
                      onClick={() => approve.mutate(job.id)}
                      disabled={approve.isPending}
                      className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 disabled:opacity-50 transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => reject.mutate(job.id)}
                      disabled={reject.isPending}
                      className="px-4 py-2 bg-red-100 text-red-700 text-sm font-medium rounded-lg hover:bg-red-200 disabled:opacity-50 transition-colors"
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
    </div>
  );
}
