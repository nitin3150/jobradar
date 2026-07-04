import { useEffect, useRef } from 'react';

export function useClickOutside(ref, handler, enabled = true) {
  const handlerRef = useRef(handler);

  // Sync the latest handler outside of render so listener attachments don't
  // change identity on every parent render.
  useEffect(() => {
    handlerRef.current = handler;
  });

  useEffect(() => {
    if (!enabled) return;
    function listener(event) {
      if (!ref.current || ref.current.contains(event.target)) return;
      handlerRef.current(event);
    }
    document.addEventListener('mousedown', listener);
    document.addEventListener('touchstart', listener);
    return () => {
      document.removeEventListener('mousedown', listener);
      document.removeEventListener('touchstart', listener);
    };
  }, [ref, enabled]);
}
