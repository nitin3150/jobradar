import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useCompany } from '../hooks/useCompanies';
import { useJobs } from '../hooks/useJobs';
import { useOutreachMessages } from '../hooks/useOutreach';
import Navbar from '../components/Navbar';
import OutreachPanel from '../components/OutreachPanel';

const JOB_STATUS_COLORS = {
  in_review: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-800',
  applied: 'bg-blue-100 text-blue-800',
  flagged: 'bg-orange-100 text-orange-800',
};

// Related Jobs list. The backend ``GET /api/jobs?company_id=<uuid>``
// filter does the SQL work; this component renders the returned
// rows as compact cards that link to ``/jobs/:id`` for the full
// detail + research view. Lazy research: we deliberately do NOT
// fetch research_reports here — an N+1 of LLM-backed queries per
// company would be too expensive. The JobDetail page handles the
// "View research" affordance on click.
function RelatedJobs({ companyId }) {
  const { data, isLoading } = useJobs({ company_id: companyId, page_size: 50 });

  if (isLoading) {
    return (
      <p className="text-sm text-gray-400">Loading related jobs…</p>
    );
  }

  const jobs = data?.jobs || [];
  if (jobs.length === 0) {
    return (
      <p className="text-sm text-gray-400">
        No jobs from this company in the queue yet.
      </p>
    );
  }

  return (
    <div className="space-y-2" data-testid="related-jobs">
      {jobs.map((job) => (
        <Link
          key={job.id}
          to={`/jobs/${job.id}`}
          className="flex items-center justify-between gap-3 p-3 bg-gray-50 hover:bg-gray-100 rounded-lg border border-gray-100 transition-colors"
        >
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-gray-900 truncate">
              {job.title}
            </p>
            <p className="text-xs text-gray-500 mt-0.5">
              {job.ats_type}
              {job.ai_fit_score != null && (
                <>
                  {' · '}
                  <span className="text-indigo-600 font-medium">
                    {Math.round(job.ai_fit_score * 100)}% fit
                  </span>
                </>
              )}
            </p>
          </div>
          <span
            className={`text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full ${
              JOB_STATUS_COLORS[job.status] || 'bg-gray-100 text-gray-700'
            }`}
          >
            {job.status.replace('_', ' ')}
          </span>
        </Link>
      ))}
    </div>
  );
}

function ScoreBar({ score }) {
  let barColor = 'bg-red-500';
  let label = 'Low';
  if (score >= 70) {
    barColor = 'bg-green-500';
    label = 'High';
  } else if (score >= 40) {
    barColor = 'bg-yellow-500';
    label = 'Medium';
  }

  return (
    <div className="flex items-center gap-3">
      <div className="w-32 h-3 bg-gray-200 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${barColor}`} style={{ width: `${score}%` }} />
      </div>
      <span className="text-sm font-semibold">{score}/100 ({label})</span>
    </div>
  );
}

export default function CompanyDetail() {
  const { id } = useParams();
  const { data: company, isLoading } = useCompany(id);
  const { data: messages } = useOutreachMessages(id);
  const [showOutreach, setShowOutreach] = useState(false);

  if (isLoading) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-4 bg-gray-100 rounded w-1/2" />
          <div className="h-40 bg-gray-100 rounded" />
        </div>
      </div>
    );
  }

  if (!company) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-8 text-center">
        <p className="text-gray-500">Company not found</p>
        <Link to="/" className="text-indigo-600 hover:underline mt-2 inline-block">Back to dashboard</Link>
      </div>
    );
  }

  return (
    <>
    <Navbar category="startup" onCategoryChange={() => {}} />
    <div className="max-w-4xl mx-auto px-4 py-8">
      {/* Back link */}
      <Link to="/" className="text-sm text-indigo-600 hover:underline mb-4 inline-block">
        &larr; Back to dashboard
      </Link>

      {/* Header */}
      <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{company.name}</h1>
            {company.website && (
              <a
                href={company.website}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-indigo-600 hover:underline"
              >
                {company.website}
              </a>
            )}
          </div>
          <button
            onClick={() => setShowOutreach(true)}
            className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700"
          >
            Generate Outreach
          </button>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <p className="text-xs text-gray-500 uppercase">Funding</p>
            <p className="font-semibold">
              {company.funding_amount ? `$${(company.funding_amount / 1e6).toFixed(1)}M` : 'Undisclosed'}
            </p>
          </div>
          <div>
            <p className="text-xs text-gray-500 uppercase">Stage</p>
            <p className="font-semibold">{company.funding_stage}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 uppercase">Source</p>
            <p className="font-semibold">{company.source}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500 uppercase">Status</p>
            <p className="font-semibold capitalize">{company.status}</p>
          </div>
        </div>

        <div className="mb-4">
          <p className="text-xs text-gray-500 uppercase mb-1">Hiring Intent Score</p>
          <ScoreBar score={company.hiring_intent_score} />
        </div>

        {company.company_summary && (
          <p className="text-gray-600 text-sm">{company.company_summary}</p>
        )}
      </div>

      {/* Likely Roles */}
      {company.likely_roles?.length > 0 && (
        <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
          <h2 className="text-sm font-semibold text-gray-900 mb-3">Likely Hiring For</h2>
          <div className="flex flex-wrap gap-2">
            {company.likely_roles.map((role, i) => (
              <span key={i} className="px-3 py-1 bg-indigo-50 text-indigo-700 rounded-full text-sm">
                {role}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Hiring Signals */}
      {company.hiring_signals?.length > 0 && (
        <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
          <h2 className="text-sm font-semibold text-gray-900 mb-3">Hiring Signals</h2>
          <div className="space-y-2">
            {company.hiring_signals.map((signal, i) => (
              <p key={i} className="text-sm text-gray-600 bg-gray-50 p-3 rounded-lg italic">
                "{signal}"
              </p>
            ))}
          </div>
        </div>
      )}

      {/* Founder Info */}
      {(company.founder_name || company.founder_twitter || company.founder_linkedin) && (
        <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
          <h2 className="text-sm font-semibold text-gray-900 mb-3">Founder</h2>
          {company.founder_name && <p className="text-sm text-gray-700 mb-1">{company.founder_name}</p>}
          <div className="flex gap-3">
            {company.founder_twitter && (
              <a
                href={`https://x.com/${company.founder_twitter.replace('@', '')}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-sky-600 hover:underline"
              >
                Twitter: {company.founder_twitter}
              </a>
            )}
            {company.founder_linkedin && (
              <a
                href={company.founder_linkedin}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-600 hover:underline"
              >
                LinkedIn
              </a>
            )}
          </div>
        </div>
      )}

      {/* Related Jobs — drives the wire from company → JobDetail.
          Each row is a Link to ``/jobs/:id`` where the operator can
          read the full posting + the LLM Interview Prep brief. */}
      <div className="bg-white border border-gray-200 rounded-xl p-6 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-900">
            Related Jobs
          </h2>
          <Link
            to={`/jobs?company_id=${company.id}`}
            className="text-xs text-indigo-600 hover:underline"
          >
            View all →
          </Link>
        </div>
        <RelatedJobs companyId={company.id} />
      </div>

      {/* Outreach History */}
      <div className="bg-white border border-gray-200 rounded-xl p-6">
        <h2 className="text-sm font-semibold text-gray-900 mb-3">
          Outreach History ({messages?.length || 0})
        </h2>
        {messages?.length > 0 ? (
          <div className="space-y-4">
            {messages.map((msg) => (
              <div key={msg.id} className="border border-gray-100 rounded-lg p-4">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-xs font-medium px-2 py-0.5 bg-gray-100 rounded-full capitalize">
                    {msg.type.replace('_', ' ')}
                  </span>
                  <span className="text-xs text-gray-400">
                    {new Date(msg.generated_at).toLocaleString()}
                  </span>
                </div>
                <p className="text-sm text-gray-700 whitespace-pre-wrap">{msg.content}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-400">No outreach messages generated yet.</p>
        )}
      </div>

      {/* Outreach Panel */}
      {showOutreach && (
        <>
          <div className="fixed inset-0 bg-black/20 z-40" onClick={() => setShowOutreach(false)} />
          <OutreachPanel company={company} onClose={() => setShowOutreach(false)} />
        </>
      )}
    </div>
    </>
  );
}
