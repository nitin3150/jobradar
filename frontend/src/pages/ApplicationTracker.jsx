import { useState } from 'react';
import Navbar from '../components/Navbar';
import { useApplications, useUpdateApplicationStatus } from '../hooks/useApplications';

const STATUS_COLORS = {
  submitted: 'bg-blue-100 text-blue-800',
  interview: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  offer: 'bg-purple-100 text-purple-800',
  ghosted: 'bg-gray-100 text-gray-600',
};

const ALL_STATUSES = ['submitted', 'interview', 'rejected', 'offer', 'ghosted'];

export default function ApplicationTracker() {
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedApp, setSelectedApp] = useState(null);
  const { data, isLoading } = useApplications({
    status: statusFilter || undefined,
    page_size: 50,
  });
  const updateStatus = useUpdateApplicationStatus();

  return (
    <>
      <Navbar />
      <div className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Application Tracker</h1>
          <div className="flex gap-2">
            <button
              onClick={() => setStatusFilter('')}
              className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                !statusFilter
                  ? 'bg-indigo-600 text-white border-indigo-600'
                  : 'bg-white text-gray-600 border-gray-300'
              }`}
            >
              All
            </button>
            {ALL_STATUSES.map((s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                  statusFilter === s
                    ? 'bg-indigo-600 text-white border-indigo-600'
                    : 'bg-white text-gray-600 border-gray-300'
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {isLoading && (
          <div className="text-center py-12 text-gray-500">Loading applications...</div>
        )}

        <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Job</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Company</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Submitted</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Last Email</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {data?.applications?.map((app) => (
                <tr key={app.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 font-medium text-gray-900">{app.job_title}</td>
                  <td className="px-4 py-3 text-gray-600">{app.company_name}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {new Date(app.submitted_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3">
                    <select
                      value={app.status}
                      onChange={(e) =>
                        updateStatus.mutate({ id: app.id, status: e.target.value })
                      }
                      className={`px-2 py-1 rounded text-xs font-medium border-0 cursor-pointer ${
                        STATUS_COLORS[app.status] || 'bg-gray-100 text-gray-700'
                      }`}
                    >
                      {ALL_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {app.last_email_at
                      ? new Date(app.last_email_at).toLocaleDateString()
                      : '—'}
                  </td>
                  <td className="px-4 py-3">
                    {app.submission_screenshot_path && (
                      <button
                        onClick={() => setSelectedApp(app)}
                        className="text-xs text-indigo-500 hover:underline"
                      >
                        Screenshot
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {!isLoading && !data?.applications?.length && (
            <div className="text-center py-12 text-gray-400">No applications yet.</div>
          )}
        </div>

        {selectedApp && (
          <div
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            onClick={() => setSelectedApp(null)}
          >
            <div
              className="bg-white rounded-xl p-4 max-w-2xl w-full mx-4"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex justify-between items-center mb-3">
                <h3 className="font-semibold text-gray-900">{selectedApp.job_title}</h3>
                <button
                  onClick={() => setSelectedApp(null)}
                  className="text-gray-400 hover:text-gray-600"
                >
                  ✕
                </button>
              </div>
              <img
                src={`/screenshots/${selectedApp.id}.png`}
                alt="Application screenshot"
                className="w-full rounded-lg border"
              />
            </div>
          </div>
        )}
      </div>
    </>
  );
}
