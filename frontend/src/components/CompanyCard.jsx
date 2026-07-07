import { useEffect, useRef, useState } from 'react';
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
        {Math.round(score ?? 0)}
      </span>
    </div>
  );
}

const SOURCE_COLORS = {
  producthunt: 'bg-red-100 text-red-700',
  startupsgallery: 'bg-purple-100 text-purple-700',
  reliefweb: 'bg-rose-100 text-rose-700',
  idealist: 'bg-emerald-100 text-emerald-700',
  unjobs: 'bg-cyan-100 text-cyan-700',
  techjobsforgood: 'bg-teal-100 text-teal-700',
  hackernews: 'bg-orange-100 text-orange-700',
  remotive: 'bg-blue-100 text-blue-700',
  remoteok: 'bg-indigo-100 text-indigo-700',
  ashby: 'bg-emerald-100 text-emerald-700',
  greenhouse: 'bg-green-100 text-green-700',
  lever: 'bg-yellow-100 text-yellow-700',
  github: 'bg-gray-900 text-white',
  github_issues: 'bg-gray-800 text-white',
};

function SourceBadge({ source }) {
  const colorClass = SOURCE_COLORS[source] || 'bg-gray-100 text-gray-700';
  const label = (source || '').toUpperCase().replace('_', ' ');
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${colorClass}`}>
      {label}
    </span>
  );
}

const DIFFICULTY_COLORS = {
  easy: 'bg-green-100 text-green-700 border-green-200',
  medium: 'bg-yellow-100 text-yellow-700 border-yellow-200',
  hard: 'bg-red-100 text-red-700 border-red-200',
};

function DifficultyBadge({ difficulty }) {
  if (!difficulty) return null;
  const colorClass = DIFFICULTY_COLORS[difficulty] || 'bg-gray-100 text-gray-700 border-gray-200';
  return (
    <span className={`text-[11px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full border ${colorClass}`}>
      {difficulty}
    </span>
  );
}

function StarsChip({ stars }) {
  if (!stars) return null;
  const formatted = stars >= 1000 ? `${(stars / 1000).toFixed(1)}k` : String(stars);
  return (
    <span className="text-xs text-gray-500 flex items-center gap-1">
      <span aria-hidden="true">★</span>
      <span>{formatted}</span>
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
}function OssExtras({ opportunity }) {
  const [copied, setCopied] = useState(false);
  // Track the reset timer so we can clear it on unmount / re-click, which
  // prevents React's "setState on unmounted component" warning.
  const timerRef = useRef(null);

  useEffect(
    () => () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    },
    [],
  );

  const flashCopied = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    setCopied(true);
    timerRef.current = setTimeout(() => {
      setCopied(false);
      timerRef.current = null;
    }, 2000);
  };

  const handleCopy = async () => {
    const subject = opportunity.reachout_subject || `Contribution inquiry: ${opportunity.title}`;
    const body = opportunity.reachout_body || '';
    const text = `Subject: ${subject}\n\n${body}`;

    // Modern path: navigator.clipboard requires a secure context (HTTPS / localhost).
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        flashCopied();
        return;
      } catch {
        // Fall through to the legacy path below.
      }
    }

    // Legacy path: select-and-copy via a hidden <textarea>. Supported across
    // every browser and degrades cleanly on non-secure-context previews.
    // Use try/finally so the textarea is guaranteed-removed even if
    // select()/execCommand() throws in an exotic browser.
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '-1000px';
    document.body.appendChild(textarea);
    let succeeded = false;
    try {
      textarea.select();
      succeeded = document.execCommand('copy');
    } catch {
      succeeded = false;
    } finally {
      if (textarea.parentNode === document.body) {
        document.body.removeChild(textarea);
      }
    }

    if (succeeded) {
      flashCopied();
    } else {
      // Final fallback: let the user read + manually copy from a prompt.
      window.prompt('Copy this outreach message (Cmd/Ctrl+C):', text);
    }
  };

  return (
    <div className="space-y-2">
      {opportunity.top_issues?.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {opportunity.top_issues.map((issue) => (
            <a
              key={issue.url || issue.number}
              href={issue.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-700 bg-blue-50 border border-blue-100 rounded-md px-2 py-1 hover:bg-blue-100 transition-colors"
            >
              <span className="font-semibold mr-1">#{issue.number}</span>
              <span className="line-clamp-1 align-middle">{issue.title}</span>
            </a>
          ))}
        </div>
      )}

      {opportunity.reachout_strategy && (
        <div className="bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
          <p className="text-[11px] uppercase tracking-wide text-gray-500 font-medium mb-1">
            Reachout strategy
          </p>
          <p className="text-xs text-gray-700 whitespace-pre-wrap leading-relaxed">
            {opportunity.reachout_strategy}
          </p>
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          onClick={handleCopy}
          aria-label="Copy maintainer outreach email to clipboard"
          className="text-xs px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
        >
          {copied ? 'Copied ✓' : 'Copy Outreach Email'}
        </button>
      </div>
    </div>
  );
}

export default function CompanyCard({ opportunity, onGenerateOutreach }) {
  const category = opportunity.category;

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            {opportunity.url ? (
              <a
                href={opportunity.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-lg font-semibold text-gray-900 hover:text-indigo-600 transition-colors truncate"
              >
                {opportunity.title}
              </a>
            ) : (
              <span className="text-lg font-semibold text-gray-900">{opportunity.title}</span>
            )}
            <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">
              {opportunity.organization}
            </span>
            {category === 'oss' && (
              <>
                <DifficultyBadge difficulty={opportunity.difficulty} />
                <StarsChip stars={opportunity.stars} />
              </>
            )}
          </div>
          <div className="flex items-center gap-2 text-sm text-gray-500 flex-wrap">
            <span>{timeAgo(opportunity.published)}</span>
            {opportunity.location && (
              <>
                <span>·</span>
                <span>{opportunity.location}</span>
              </>
            )}
            {opportunity.primary_language && (
              <>
                <span>·</span>
                <span className="text-xs font-medium text-gray-600">
                  {opportunity.primary_language}
                </span>
              </>
            )}
          </div>
        </div>
        <ScoreBadge score={opportunity.score * 100} />
      </div>

      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <SourceBadge source={opportunity.source} />
        {(opportunity.tags || []).slice(0, 4).map((tag) => (
          <span key={tag} className="text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full">
            {tag}
          </span>
        ))}
      </div>

      {opportunity.description && (
        <p className="text-sm text-gray-600 mb-3 line-clamp-3">{opportunity.description}</p>
      )}

      {category === 'oss' && (
        <div className="mb-3">
          <OssExtras opportunity={opportunity} />
        </div>
      )}

      <div className="flex items-center justify-between pt-3 border-t border-gray-100">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-gray-500 text-xs">{category.toUpperCase()}</span>
        </div>
        <div className="flex items-center gap-2">
          {onGenerateOutreach && category === 'boards' && (
            <button
              onClick={() => onGenerateOutreach(opportunity)}
              className="text-xs px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
            >
              Generate Outreach
            </button>
          )}
          <a
            href={opportunity.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-indigo-500 hover:underline"
          >
            View →
          </a>
        </div>
      </div>
    </div>
  );
}
