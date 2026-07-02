import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateCompanyStatus } from '../api/client';
import { Link } from 'react-router-dom';

function ScoreBadge({ score }) {
  let color = 'bg-red-100 text-red-700 border-red-200';
  let barColor = 'bg-red-500';
  if (score >= 70) {
    color = 'bg-green-100 text-green-700 border-green-200';
    barColor = 'bg-green-500';
  } else if (score >= 40) {
    color = 'bg-yellow-100 text-yellow-700 border-yellow-200';
    barColor = 'bg-yellow-500';
  }

  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-2 bg-gray-200 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${barColor}`} style={{ width: `${score}%` }} />
      </div>
      <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${color}`}>
        {score}
      </span>
    </div>
  );
}

function SourceBadge({ source }) {
  const colors = {
    sec_edgar: 'bg-blue-100 text-blue-700',
    yc: 'bg-orange-100 text-orange-700',
    crunchbase: 'bg-purple-100 text-purple-700',
    twitter: 'bg-sky-100 text-sky-700',
    hackernews: 'bg-orange-100 text-orange-700',
    techcrunch: 'bg-green-100 text-green-700',
    producthunt: 'bg-red-100 text-red-700',
    idealist: 'bg-emerald-100 text-emerald-700',
    unjobs: 'bg-cyan-100 text-cyan-700',
    techjobsforgood: 'bg-teal-100 text-teal-700',
    reliefweb: 'bg-rose-100 text-rose-700',
  };
  const label = source.startsWith('vc_') ? source.replace('vc_', '').toUpperCase() : source.toUpperCase().replace('_', ' ');
  const colorClass = colors[source] || 'bg-gray-100 text-gray-700';

  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${colorClass}`}>
      {label}
    </span>
  );
}

function timeAgo(dateStr) {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const days = Math.floor(diff / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return '1 day ago';
  if (days < 30) return `${days} days ago`;
  return new Date(dateStr).toLocaleDateString();
}

export default function CompanyCard({ company, onGenerateOutreach }) {
  const queryClient = useQueryClient();
  const statusMutation = useMutation({
    mutationFn: ({ id, status }) => updateCompanyStatus(id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['companies'] }),
  });

  const isNgo = company.category === 'ngo';
  const fundingText = company.funding_amount
    ? `$${(company.funding_amount / 1e6).toFixed(1)}M`
    : 'Undisclosed';

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <Link
              to={`/company/${company.id}`}
              className="text-lg font-semibold text-gray-900 hover:text-indigo-600 transition-colors"
            >
              {company.name}
            </Link>
            {isNgo && (
              <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">
                Nonprofit
              </span>
            )}
            {company.website && (
              <a
                href={company.website}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-gray-400 hover:text-gray-600"
              >
                &#8599;
              </a>
            )}
          </div>
          <div className="flex items-center gap-2 text-sm text-gray-500">
            {isNgo ? (
              <>
                {company.likely_roles?.[0] && (
                  <span className="font-medium text-gray-700">{company.likely_roles[0]}</span>
                )}
                <span>·</span>
                <span>{timeAgo(company.created_at)}</span>
              </>
            ) : (
              <>
                <span className="font-medium text-gray-700">
                  Raised {fundingText} {company.funding_stage !== 'unknown' && company.funding_stage}
                </span>
                <span>·</span>
                <span>{timeAgo(company.funding_date || company.created_at)}</span>
              </>
            )}
          </div>
        </div>
        <ScoreBadge score={company.hiring_intent_score} />
      </div>

      <div className="flex items-center gap-2 mb-3">
        <SourceBadge source={company.source} />
        {company.likely_roles?.slice(0, 3).map((role, i) => (
          <span key={i} className="text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full">
            {role}
          </span>
        ))}
      </div>

      {company.hiring_signals?.length > 0 && (
        <div className="mb-3 space-y-1">
          {company.hiring_signals.slice(0, 2).map((signal, i) => (
            <p key={i} className="text-xs text-gray-500 italic line-clamp-1">
              "{signal}"
            </p>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between pt-3 border-t border-gray-100">
        <div className="flex items-center gap-3 text-sm">
          {company.founder_name && (
            <span className="text-gray-600">{company.founder_name}</span>
          )}
          {company.founder_twitter && (
            <a
              href={`https://x.com/${company.founder_twitter.replace('@', '')}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sky-500 hover:text-sky-600"
            >
              {company.founder_twitter}
            </a>
          )}
        </div>

        <div className="flex items-center gap-2">
          <select
            value={company.status}
            onChange={(e) => statusMutation.mutate({ id: company.id, status: e.target.value })}
            className="text-xs border border-gray-200 rounded-lg px-2 py-1 bg-white"
          >
            <option value="new">New</option>
            <option value="contacted">Contacted</option>
            <option value="interviewing">Interviewing</option>
            <option value="pass">Pass</option>
          </select>
          <button
            onClick={() => onGenerateOutreach(company)}
            className="text-xs px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
          >
            Generate Outreach
          </button>
        </div>
      </div>
    </div>
  );
}
