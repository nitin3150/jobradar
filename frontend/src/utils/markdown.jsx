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
//
// Originally lived as a private helper inside
// ``InterviewPrepModal.jsx``; extracted to ``utils/markdown.js`` so
// ``JobDetail`` can render the same LLM brief inline on the page
// without re-implementing the parser. The DOM structure (h2 + ul + p
// with ``text-sm text-gray-700`` styling) is preserved verbatim so
// both surfaces look identical.

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

export function renderMarkdown(md) {
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
