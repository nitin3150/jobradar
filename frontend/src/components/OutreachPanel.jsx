import { useState, useEffect } from 'react';
import { useGenerateOutreach } from '../hooks/useOutreach';

const STORAGE_KEY = 'fundingradar_user_context';

function loadUserContext() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved ? JSON.parse(saved) : { name: '', role: '', skills: '', background: '' };
  } catch {
    return { name: '', role: '', skills: '', background: '' };
  }
}

export default function OutreachPanel({ company, onClose }) {
  const [type, setType] = useState('email');
  const [userContext, setUserContext] = useState(loadUserContext);
  const [generatedMessage, setGeneratedMessage] = useState('');
  const [copied, setCopied] = useState(false);

  const generateMutation = useGenerateOutreach();

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(userContext));
  }, [userContext]);

  // Reset the previously generated message when the active company changes.
  // Done during render (not in an Effect) per the React docs pattern for
  // "adjusting state when a prop changes" — avoids the cascading-render lint.
  const [lastCompanyId, setLastCompanyId] = useState(company?.id ?? null);
  const currentCompanyId = company?.id ?? null;
  if (currentCompanyId !== lastCompanyId) {
    setLastCompanyId(currentCompanyId);
    setGeneratedMessage('');
  }

  const handleGenerate = async () => {
    const skills = userContext.skills
      ? userContext.skills.split(',').map((s) => s.trim()).filter(Boolean)
      : [];

    const result = await generateMutation.mutateAsync({
      company_id: company.id,
      type,
      user_context: { ...userContext, skills },
    });
    setGeneratedMessage(result.content);
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(generatedMessage);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (!company) return null;

  return (
    <div className="fixed inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl border-l border-gray-200 z-50 flex flex-col">
      {/* Header */}
      <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">Generate Outreach</h2>
          <p className="text-sm text-gray-500">{company.name}</p>
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-2xl leading-none">
          &times;
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {/* Company Summary */}
        {company.company_summary && (
          <div className="bg-gray-50 rounded-lg p-3">
            <p className="text-sm text-gray-600">{company.company_summary}</p>
          </div>
        )}

        {/* Hiring Signals */}
        {company.hiring_signals?.length > 0 && (
          <div>
            <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Hiring Signals</h3>
            <div className="space-y-1">
              {company.hiring_signals.map((signal, i) => (
                <p key={i} className="text-xs text-gray-500 italic">"{signal}"</p>
              ))}
            </div>
          </div>
        )}

        {/* Type Toggle */}
        <div>
          <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Message Type</h3>
          <div className="flex gap-2">
            {['email', 'twitter_dm', 'linkedin'].map((t) => (
              <button
                key={t}
                onClick={() => setType(t)}
                className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${
                  type === t
                    ? 'bg-indigo-600 text-white border-indigo-600'
                    : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-50'
                }`}
              >
                {t === 'twitter_dm' ? 'Twitter DM' : t.charAt(0).toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
        </div>

        {/* User Context */}
        <div className="space-y-3">
          <h3 className="text-xs font-medium text-gray-500 uppercase">Your Info</h3>
          <input
            placeholder="Your name"
            value={userContext.name}
            onChange={(e) => setUserContext((p) => ({ ...p, name: e.target.value }))}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
          />
          <input
            placeholder="Current role"
            value={userContext.role}
            onChange={(e) => setUserContext((p) => ({ ...p, role: e.target.value }))}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
          />
          <input
            placeholder="Top skills (comma separated)"
            value={userContext.skills}
            onChange={(e) => setUserContext((p) => ({ ...p, skills: e.target.value }))}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
          />
          <textarea
            placeholder="Brief background (optional)"
            value={userContext.background}
            onChange={(e) => setUserContext((p) => ({ ...p, background: e.target.value }))}
            rows={2}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm resize-none"
          />
        </div>

        {/* Generate Button */}
        <button
          onClick={handleGenerate}
          disabled={generateMutation.isPending}
          className="w-full py-2.5 bg-indigo-600 text-white font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
        >
          {generateMutation.isPending ? 'Generating...' : 'Generate Message'}
        </button>

        {/* Generated Message */}
        {generatedMessage && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-medium text-gray-500 uppercase">Generated Message</h3>
              <button
                onClick={handleCopy}
                className="text-xs text-indigo-600 hover:text-indigo-700 font-medium"
              >
                {copied ? 'Copied!' : 'Copy'}
              </button>
            </div>
            <textarea
              value={generatedMessage}
              onChange={(e) => setGeneratedMessage(e.target.value)}
              rows={10}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm resize-y"
            />
          </div>
        )}

        {generateMutation.isError && (
          <p className="text-sm text-red-600">Failed to generate message. Please try again.</p>
        )}
      </div>
    </div>
  );
}
