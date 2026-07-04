import { useRef, useState } from 'react';
import Modal from '../Modal';
import { useResumes, formatBytes } from '../../hooks/useResumes';

const MAX_BYTES = 10 * 1024 * 1024;

function FileIcon() {
  return (
    <svg
      className="w-5 h-5 text-indigo-500 shrink-0"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}

function ProgressBar({ pct }) {
  const safe = Math.max(0, Math.min(100, pct || 0));
  return (
    <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
      <div
        className="h-full bg-indigo-500 transition-[width] duration-150"
        style={{ width: `${safe}%` }}
      />
    </div>
  );
}

function TagEditor({ resume, onCommit, busy }) {
  const [text, setText] = useState((resume.tags || []).join(', '));
  return (
    <input
      type="text"
      placeholder="tags (comma separated, e.g. ml, backend)"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        const next = text.split(',').map((s) => s.trim()).filter(Boolean);
        const cur = resume.tags || [];
        if (next.length === cur.length && next.every((t, i) => t === cur[i])) {
          return;
        }
        onCommit(next);
      }}
      disabled={busy}
      className="w-full text-xs border border-gray-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-indigo-400 disabled:bg-gray-50 disabled:opacity-70"
    />
  );
}

function ResumeRow({ resume, onToggleDefault, onRemove, onTagsCommit, updating, removing, busyUpload }) {
  return (
    <div className="flex flex-col gap-2 bg-gray-50 border border-gray-200 rounded-lg p-3">
      <div className="flex items-center gap-3">
        <FileIcon />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-gray-900 truncate">{resume.name}</div>
          <div className="text-xs text-gray-500">
            {formatBytes(resume.size_bytes)} ·
            uploaded {new Date(resume.uploaded_at).toLocaleDateString()} ·
            <a
              href={`/api/resumes/${resume.id}/download`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-500 hover:underline ml-1"
            >
              Download
            </a>
          </div>
        </div>
        <label className="flex items-center gap-1 text-xs text-gray-600 whitespace-nowrap">
          <input
            type="checkbox"
            checked={!!resume.is_default}
            disabled={updating || busyUpload}
            onChange={(e) => onToggleDefault(resume.id, e.target.checked)}
            className="accent-indigo-600 disabled:opacity-50"
          />
          Default
        </label>
        <button
          onClick={() => onRemove(resume.id)}
          disabled={removing || busyUpload}
          className="text-xs text-red-500 hover:text-red-700 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-50"
        >
          {removing ? '…' : 'Remove'}
        </button>
      </div>
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <TagEditor
            resume={resume}
            busy={updating || busyUpload}
            onCommit={(tags) => onTagsCommit(resume.id, tags)}
          />
        </div>
      </div>
    </div>
  );
}

export default function ResumesModal({ open, onClose }) {
  const {
    resumes,
    isLoading,
    uploadAsync,
    isUploading,
    uploadError,
    remove,
    isRemoving,
    update,
    isUpdating,
  } = useResumes();
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState('');
  const [progressLabel, setProgressLabel] = useState('');
  const [activeProgress, setActiveProgress] = useState(0);

  const resetProgressUi = () => {
    setProgressLabel('');
    setActiveProgress(0);
  };

  const handleFiles = async (fileList) => {
    setError('');
    resetProgressUi();
    const files = Array.from(fileList || []);
    if (!files.length) return;

    for (const f of files) {
      if (f.size > MAX_BYTES) {
        setError(`"${f.name}" is over ${MAX_BYTES / 1024 / 1024} MB. Trim or upload a smaller version.`);
        return;
      }
    }

    // Sequential uploads keep the UI sane (single progress bar).
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      setProgressLabel(`Uploading ${i + 1} of ${files.length}: ${f.name}`);
      try {
        await uploadAsync({
          file: f,
          onProgress: (e) => {
            if (e.total) setActiveProgress(Math.round((e.loaded / e.total) * 100));
          },
        });
        setActiveProgress(0);
      } catch (e) {
        const msg =
          e?.response?.status === 413
            ? `"${f.name}" is too large — backend rejected it.`
            : e?.response?.status === 415
            ? `"${f.name}" has an unsupported file type.`
            : `Couldn't upload "${f.name}": ${e?.message || 'unknown error'}`;
        setError(msg);
        setActiveProgress(0);
        return;
      }
    }
    setProgressLabel('');
    if (inputRef.current) inputRef.current.value = '';
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Resumes"
      description="Upload multiple versions. The apply_worker will pick the best tag match per role."
      widthClass="max-w-2xl"
      footer={
        <div className="flex items-center justify-between text-xs text-gray-500">
          <span>
            {isLoading ? 'Loading…' : `${resumes.length} resume${resumes.length === 1 ? '' : 's'} on file`}
          </span>
          <span>Server-backed · max 10 MB each</span>
        </div>
      }
    >
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        className={`border-2 border-dashed rounded-xl px-6 py-8 text-center cursor-pointer transition-colors ${
          dragging
            ? 'border-indigo-500 bg-indigo-50'
            : 'border-gray-300 hover:border-indigo-400 hover:bg-gray-50'
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.doc,.docx,.txt,.md"
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />
        <div className="text-sm font-medium text-gray-700">
          {isUploading ? progressLabel || 'Uploading…' : 'Drop resumes here or click to browse'}
        </div>
        <div className="text-xs text-gray-400 mt-1">PDF, DOC, DOCX, TXT, MD — up to 10 MB each</div>
        {isUploading && (
          <div className="mt-3">
            <ProgressBar pct={activeProgress} />
          </div>
        )}
      </div>

      {error && (
        <p className="mt-3 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          {error}
        </p>
      )}
      {uploadError && !error && (
        <p className="mt-3 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          Upload failed: {String(uploadError.message || uploadError)}
        </p>
      )}

      <div className="mt-5 space-y-2">
        {!isLoading && resumes.length === 0 && (
          <p className="text-sm text-gray-400 italic">No resumes uploaded yet.</p>
        )}
        {resumes.map((r) => (
          <ResumeRow
            key={r.id}
            resume={r}
            busyUpload={isUploading}
            updating={isUpdating}
            removing={isRemoving}
            onToggleDefault={(id, isDefault) => update({ id, patch: { is_default: isDefault } })}
            onTagsCommit={(id, tags) => update({ id, patch: { tags } })}
            onRemove={(id) => {
              if (confirm(`Remove this resume? This can't be undone.`)) {
                remove(id);
              }
            }}
          />
        ))}
      </div>
    </Modal>
  );
}
