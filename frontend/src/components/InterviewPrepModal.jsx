import { useEffect, useState } from 'react';
import Modal from './Modal';

// Tiny Markdown -> HTML renderer. v1 inline implementation; the report
// content paths are well-bounded (LLM-emitted bullets + headings), so
// a heavyweight renderer like react-markdown is unnecessary. Renders
// the five-section structure the backend ``RESEARCH_SYSTEM_PROMPT``
// asks for (Company Snapshot / Likely Tech Stack / What they probably
// test / 5 smart questions / Red flags).
//
// Sanitization model:
// - We don't try to escape every conceivable injection. The content
//   comes from the operator's own LLM call against a system prompt
//   we control. We render headings/lists/paragraphs and leave
//   inline-formatting markup as plain text. If a future hostile
//   source ever flows through here, swap this in for ``react-markdown``
//   + ``rehype-sanitize``.

function escape(s) {
  return s
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function renderInline(text, keyFn) {
  const parts = [];
  // ``**bold**`` -> <strong>…</strong>
  const boldRe = /\*\*([^*]+)\*\*/g;
  let last = 0;
  let m;
  let i = 0;
  while ((m = boldRe.exec(text)) !== null) {
    if (m.index > last) parts.push(<span key={`t-${keyFn(i++)}`}>{escape(text.slice(last, m.index))}</span>);
    parts.push(<strong key={`b-${keyFn(i++)}`}>{escape(m[1])}</strong>);
    last = boldRe.lastIndex;
  }
  if (last < text.length) parts.push(<span key={`t-${keyFn(i++)}`}>{escape(text.slice(last))}</span>);
  return parts;
}

function renderMarkdown(md) {
  if (!md) return [];
  const blocks = [];
  const lines = md.split('\n');
  let i = 0;
  let blockKey = 0;
  while (i < lines.length) {
    const line = lines[i];
    // Heading: ``## …`` (the prompt asks for ## headings; we also
    // accept single-# just in case the model drifts).
    const h2 = line.match(/^##\s+(.+)$/);
    const h1 = line.match(/^#\s+(.+)$/);
    if (h2 || h1) {
      const text = (h2 || h1)[1];
      blocks.push(
        <h2 key={blockKey++} className="text-base font-semibold text-gray-900 mt-4 mb-2 first:mt-0">
          {renderInline(text, (k) => `h-${blockKey}-${k}`)}
        </h2>,
      );
      i += 1;
      continue;
    }
    // List: ``- …`` consecutive lines.
    if (line.match(/^[-*]\s+/)) {
      const items = [];
      while (i < lines.length && lines[i].match(/^[-*]\s+/)) {
        const li = lines[i].replace(/^[-*]\s+/, '');
        items.push(
          <li key={blockKey++} className="text-sm text-gray-700 leading-relaxed">
            {renderInline(li, (k) => `li-${blockKey}-${k}`)}
          </li>,
        );
        i += 1;
      }
      blocks.push(<ul key={blockKey++} className="list-disc ml-6 space-y-1 mb-3">{items}</ul>);
      continue;
    }
    // Blank line: paragraph separator.
    if (line.trim() === '') {
      i += 1;
      continue;
    }
    // Paragraph: gather contiguous non-blank, non-heading, non-list lines.
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !lines[i].match(/^#+\s+/) &&
      !lines[i].match(/^[-*]\s+/)
    ) {
      para.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <p key={blockKey++} className="text-sm text-gray-700 leading-relaxed mb-3">
        {renderInline(para.join(' '), (k) => `p-${blockKey}-${k}`)}
      </p>,
    );
  }
  return blocks;
}

function fmtTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString();
}

/**
 * Modal that hosts the Interview Prep flow. v1 is synchronous: the
 * button in :class:`JobCard` flips a piece of state on the parent
 * page, which opens this modal, which fires ``requestResearch`` on
 * mount. While the mutation is in-flight we render a spinner;
 * ``ready`` renders the Markdown body in the tiny inline renderer
 * above; ``failed`` surfaces the error verbatim so the operator can
 * decide whether to retry. ``onClose`` flips everything back to idle.
 *
 * ``job`` is the active job being researched; we keep it on the
 * modal so closing + reopening a different job resets ``status`` to
 * idle rather than rendering the previous job's stale content.
 */
export default function InterviewPrepModal({ open, job, mutation, onClose }) {
  const [hasAutoFired, setHasAutoFired] = useState(false);

  // Auto-fire the LLM call the moment the modal opens. ``openedJob``
  // tracks which job this modal session is for so closing +
  // re-opening for a different job resets ``hasAutoFired`` correctly.
  const [openedJob, setOpenedJob] = useState(null);
  useEffect(() => {
    if (!open || !job) return;
    if (openedJob === job.id) return;  // same job, already fired
    setOpenedJob(job.id);
    setHasAutoFired(true);
    mutation.mutate(job.id);
    mutation.reset();
  }, [open, job, openedJob, mutation]);

  // ``mutation.reset`` IS available on React Query mutations (it
  // clears data/error/pending without firing).
  const cleanOnClose = () => {
    setOpenedJob(null);
    setHasAutoFired(false);
    mutation.reset();
    onClose?.();
  };

  const status = mutation.isPending
    ? 'pending'
    : mutation.isError
    ? 'failed'
    : mutation.data?.status === 'failed'
    ? 'failed'
    : mutation.data?.status === 'ready' || mutation.data?.content
    ? 'ready'
    : 'idle';

  const headerTitle = job ? `Interview Prep — ${job.company_name || job.title}` : 'Interview Prep';
  const headerDescription = job?.title ? job.title : 'LLM-synthesised pre-interview brief.';

  return (
    <Modal
      open={open}
      onClose={cleanOnClose}
      title={headerTitle}
      description={headerDescription}
      widthClass="max-w-2xl"
      footer={
        status === 'ready' && mutation.data?.content ? (
          <div className="flex items-center justify-between">
            <div className="text-xs text-gray-500">
              {mutation.data?.model_used && (
                <span>
                  Generated by <span className="font-mono">{mutation.data.model_used}</span>
                  {mutation.data?.generated_at && ` · ${fmtTime(mutation.data.generated_at)}`}
                </span>
              )}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => mutation.mutate(job?.id)}
                disabled={mutation.isPending}
                className="px-3 py-1.5 text-xs text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
              >
                Regenerate
              </button>
              <button
                type="button"
                onClick={cleanOnClose}
                className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded-lg hover:bg-indigo-700"
              >
                Close
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={cleanOnClose}
            className="px-3 py-1.5 text-xs text-gray-700 ml-auto"
          >
            Cancel
          </button>
        )
      }
    >
      {status === 'idle' && (
        <p className="text-sm text-gray-500">Preparing research request…</p>
      )}

      {status === 'pending' && (
        <div className="flex flex-col items-center justify-center py-12 gap-3" data-testid="research-spinner">
          <span className="w-8 h-8 border-4 border-indigo-200 border-t-indigo-600 rounded-full animate-spin" />
          <p className="text-sm text-gray-600">
            Generating research brief — typically 10–60 seconds.
          </p>
        </div>
      )}

      {status === 'failed' && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-red-700 mb-1">Research failed</h3>
          <p className="text-sm text-red-700 whitespace-pre-wrap">
            {mutation.data?.error || mutation.error?.message || 'Unknown error.'}
          </p>
          <button
            type="button"
            onClick={() => mutation.mutate(job?.id)}
            className="mt-3 text-xs px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700"
          >
            Retry
          </button>
        </div>
      )}

      {status === 'ready' && mutation.data?.content && (
        <div data-testid="research-content">{renderMarkdown(mutation.data.content)}</div>
      )}
    </Modal>
  );
}
