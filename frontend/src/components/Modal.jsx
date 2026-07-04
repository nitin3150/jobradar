import { useEffect } from 'react';

// Module-level counters so multiple modals (sequential or interleaved)
// don't leave body scroll locked when they close in non-LIFO order.
let bodyLockCount = 0;

/**
 * Centered modal with backdrop click + ESC-to-close.
 * - Closes when clicking the backdrop or pressing ESC.
 * - Locks body scroll while open.
 * - Click inside the panel is non-closing.
 */
export default function Modal({
  open,
  onClose,
  title,
  description,
  children,
  widthClass = 'max-w-lg',
  footer,
}) {
  useEffect(() => {
    if (!open) return;

    function onKey(event) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);

    bodyLockCount += 1;
    if (bodyLockCount === 1) {
      const prev = document.body.style.overflow;
      document.body.dataset.modalPrevOverflow = prev;
      document.body.style.overflow = 'hidden';
    }

    return () => {
      document.removeEventListener('keydown', onKey);
      bodyLockCount = Math.max(0, bodyLockCount - 1);
      if (bodyLockCount === 0) {
        const prev = document.body.dataset.modalPrevOverflow ?? '';
        document.body.style.overflow = prev;
        delete document.body.dataset.modalPrevOverflow;
      }
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 anim-fade"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title || 'Dialog'}
    >
      <div
        className={`bg-white rounded-2xl shadow-2xl w-full ${widthClass} flex flex-col max-h-[90vh] overflow-hidden`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-gray-100">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
            {description && (
              <p className="text-sm text-gray-500 mt-0.5">{description}</p>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-gray-400 hover:text-gray-700 transition-colors text-2xl leading-none px-1 -mr-1"
          >
            ×
          </button>
        </div>
        <div className="px-6 py-5 overflow-y-auto flex-1">{children}</div>
        {footer && (
          <div className="px-6 py-3 border-t border-gray-100 bg-gray-50">{footer}</div>
        )}
      </div>
    </div>
  );
}
