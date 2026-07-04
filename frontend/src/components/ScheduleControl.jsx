import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchSchedule, updateSchedule, triggerDiscovery } from '../api/client';
import { useState } from 'react';

const LABELS = {
  1: 'Every hour',
  2: 'Every 2 hours',
  4: 'Every 4 hours',
  6: 'Every 6 hours',
  12: 'Every 12 hours',
  24: 'Once a day',
};

export default function ScheduleControl() {
  const queryClient = useQueryClient();
  const [discoverMsg, setDiscoverMsg] = useState('');

  const { data, isLoading } = useQuery({
    queryKey: ['schedule'],
    queryFn: fetchSchedule,
    staleTime: 30000,
  });

  const mutation = useMutation({
    mutationFn: updateSchedule,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['schedule'] }),
  });

  const discover = useMutation({
    mutationFn: triggerDiscovery,
    onMutate: () => setDiscoverMsg('Searching job boards…'),
    onSuccess: (res) =>
      setDiscoverMsg(
        res.status === 'completed'
          ? `Attached ${res.companies_attached} new companies`
          : `Failed: ${res.error || 'unknown error'}`
      ),
    onError: (e) => setDiscoverMsg(`Failed: ${e.message}`),
  });

  const options = data?.options || [1, 2, 4, 6, 12, 24];
  const current = data?.interval_hours ?? 1;
  const nextRun = data?.next_run ? new Date(data.next_run).toLocaleString() : null;

  return (
    <div className="flex flex-wrap items-center gap-3 mb-4 p-3 bg-gray-50 border border-gray-200 rounded-lg">
      <label className="text-sm font-medium text-gray-700">Job search frequency</label>
      <select
        className="text-sm border border-gray-300 rounded-lg px-2 py-1.5 bg-white disabled:opacity-50"
        value={current}
        disabled={isLoading || mutation.isPending}
        onChange={(e) => mutation.mutate(Number(e.target.value))}
      >
        {options.map((h) => (
          <option key={h} value={h}>
            {LABELS[h] || `Every ${h} hours`}
          </option>
        ))}
      </select>
      {mutation.isPending && <span className="text-xs text-gray-500">Saving…</span>}
      {nextRun && <span className="text-xs text-gray-500">Next run: {nextRun}</span>}

      <div className="ml-auto flex items-center gap-2">
        {discoverMsg && <span className="text-xs text-gray-500">{discoverMsg}</span>}
        <button
          onClick={() => discover.mutate()}
          disabled={discover.isPending}
          className="px-3 py-1.5 text-sm border border-gray-300 rounded-lg bg-white hover:bg-gray-50 disabled:opacity-50"
        >
          {discover.isPending ? 'Discovering…' : 'Discover boards'}
        </button>
      </div>
    </div>
  );
}
