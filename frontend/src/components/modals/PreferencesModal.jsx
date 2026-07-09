import { useState } from 'react';
import Modal from '../Modal';
import {
  usePreferences,
  DEFAULT_PREFERENCES,
  SENIORITY_TIERS,
} from '../../hooks/usePreferences';

// Post-merge cleanup: the ``EditableList`` for ``target_roles`` is
// removed — the field is no longer the source of truth (the
// operator edits roles via ``config/profile.yml``). The component
// was only used here, so the whole helper is gone.

function PreferencesForm({ prefs, busy, saveError, onSave, onClose, onReset }) {
  const [draft, setDraft] = useState(prefs);
  const [saved, setSaved] = useState(false);

  const update = (patch) => setDraft((d) => ({ ...d, ...patch }));

  const handleSave = async () => {
    try {
      // Post-merge cleanup: ``target_roles`` is legacy (the
      // profile.yml is the source of truth). Strip it from the
      // PATCH payload so a save doesn't accidentally overwrite a
      // pre-cleanup value the operator had in ``_PREFS_STATE`` —
      // and so the "Reset to defaults" button (which sets
      // ``draft`` from ``DEFAULT_PREFERENCES`` where
      // ``target_roles === []``) doesn't wipe that legacy value
      // either. The server's PATCH handler still accepts the
      // field for back-compat; this is purely a UI-side guard.
      // eslint-disable-next-line no-unused-vars
      const { target_roles: _legacy, ...payload } = draft;
      await onSave(payload);
      setSaved(true);
      setTimeout(onClose, 700);
    } catch {
      // saveError surfaces inline below.
    }
  };

  return (
    <div className="space-y-5">
      <section className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-800 mb-1">
            Review window (hours)
          </label>
          <input
            type="number"
            min={0.5}
            max={48}
            step={0.5}
            value={draft.review_window_hours}
            onChange={(e) => update({ review_window_hours: Number(e.target.value) || 1 })}
            disabled={busy}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-50 disabled:opacity-70"
          />
          <p className="text-xs text-gray-400 mt-1">
            How long you have to approve a job before the deadline action runs.
          </p>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-800 mb-1">
            Minimum AI fit score
          </label>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={draft.job_fit_threshold}
              onChange={(e) => update({ job_fit_threshold: Number(e.target.value) })}
              disabled={busy}
              className="flex-1 accent-indigo-600 disabled:opacity-70"
            />
            <span className="text-sm font-mono w-12 text-right">
              {(draft.job_fit_threshold * 100).toFixed(0)}%
            </span>
          </div>
          <p className="text-xs text-gray-400 mt-1">
            Jobs below this score are dropped silently.
          </p>
        </div>
      </section>

      <section>
        <label className="block text-sm font-medium text-gray-800 mb-1">
          Seniority band
        </label>
        <p className="text-xs text-gray-500 mb-2">
          Filter jobs before they reach the LLM scorer. <em>Any seniority</em> disables the band.
        </p>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs text-gray-600 mb-1">Minimum</label>
            <select
              value={draft.min_seniority ?? ''}
              onChange={(e) =>
                update({ min_seniority: e.target.value === '' ? null : e.target.value })
              }
              disabled={busy}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-50 disabled:opacity-70 bg-white"
            >
              {SENIORITY_TIERS.map((t) => (
                <option key={`min-${t.value || 'any'}`} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">Maximum</label>
            <select
              value={draft.max_seniority ?? ''}
              onChange={(e) =>
                update({ max_seniority: e.target.value === '' ? null : e.target.value })
              }
              disabled={busy}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-50 disabled:opacity-70 bg-white"
            >
              {SENIORITY_TIERS.map((t) => (
                <option key={`max-${t.value || 'any'}`} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </section>

      <section className="flex items-start gap-3 p-3 bg-gray-50 border border-gray-200 rounded-lg">
        <input
          id="pref-followup"
          type="checkbox"
          checked={draft.send_followup_emails}
          onChange={(e) => update({ send_followup_emails: e.target.checked })}
          disabled={busy}
          className="mt-1 accent-indigo-600 disabled:opacity-70"
        />
        <label htmlFor="pref-followup" className="block text-sm text-gray-800">
          Send a polite follow-up email 5 days after applying if there's been no reply.
          <span className="block text-xs text-gray-500 mt-0.5">
            Connects via Gmail connector — leave OAuth running.
          </span>
        </label>
      </section>

      {saveError && (
        <p className="text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          Couldn't save — {String(saveError.message || saveError)}
        </p>
      )}

      <div className="flex items-center justify-between pt-3 border-t border-gray-100">
        <button
          type="button"
          onClick={onReset}
          disabled={busy}
          className="text-xs text-gray-500 hover:text-gray-800 underline disabled:opacity-50"
        >
          Reset to defaults
        </button>
        <div className="flex items-center gap-3">
          {saved && !saveError && <span className="text-xs text-green-600">Saved</span>}
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={busy}
            className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {busy ? 'Saving…' : 'Save preferences'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function PreferencesModal({ open, onClose }) {
  const { prefs, isLoading, save, isSaving, saveError } = usePreferences();
  // Reset must force a remount so draft initializes from defaults instead of
  // the server-saved prefs. We bump this counter on Reset clicks.
  const [resetVersion, setResetVersion] = useState(0);
  // Track the previous `open` so we can drop the reset version on every
  // (re)open — otherwise a stale Reset would silently default-fill the form
  // the next time the user reopens the modal. Uses React's render-phase
  // "adjust state when a prop changes" pattern (state-based, no ref touched).
  const [lastOpen, setLastOpen] = useState(open);
  if (open !== lastOpen) {
    setLastOpen(open);
    setResetVersion(0);
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Preferences"
      description={
        isLoading
          ? 'Loading your preferences from the server…'
          : 'Tune how the pipeline searches, scores, and acts on jobs.'
      }
      widthClass="max-w-2xl"
    >
      {open && (
        <PreferencesForm
          key={`${String(open)}:${resetVersion}`}
          prefs={resetVersion > 0 ? DEFAULT_PREFERENCES : prefs}
          busy={isSaving}
          saveError={saveError}
          onSave={save}
          onClose={onClose}
          onReset={() => setResetVersion((v) => v + 1)}
        />
      )}
    </Modal>
  );
}
