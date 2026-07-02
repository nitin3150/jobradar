export default function StatusTracker({ stats }) {
  const items = [
    { label: 'Total Companies', value: stats?.total_companies ?? 0, color: 'text-gray-900' },
    { label: 'New Today', value: stats?.new_today ?? 0, color: 'text-blue-600' },
    { label: 'High Intent (70+)', value: stats?.high_intent ?? 0, color: 'text-green-600' },
    { label: 'Contacted', value: stats?.contacted ?? 0, color: 'text-purple-600' },
    { label: 'NGO Jobs', value: stats?.ngo_count ?? 0, color: 'text-emerald-600' },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
      {items.map((item) => (
        <div key={item.label} className="bg-white border border-gray-200 rounded-xl p-4">
          <p className="text-xs font-medium text-gray-500 uppercase">{item.label}</p>
          <p className={`text-2xl font-bold mt-1 ${item.color}`}>{item.value}</p>
        </div>
      ))}
    </div>
  );
}
