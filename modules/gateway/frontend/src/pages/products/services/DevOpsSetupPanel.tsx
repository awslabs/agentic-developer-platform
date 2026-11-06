import { useEffect, useId, useRef, type ReactNode } from 'react';

/** Keep drafts and uncertain requests mounted while native dialog handles focus and Escape. */
export default function DevOpsSetupPanel({ title, open, onClose, children, busy = false }: {
  title: string; open: boolean; onClose: () => void; children: ReactNode; busy?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
    if (open) {
      const previous = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      return () => { document.body.style.overflow = previous; };
    }
  }, [open]);
  return <dialog ref={dialog} hidden={!open} className="devops-setup-dialog" aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <header><h2 id={titleId}>{title}</h2><button type="button" disabled={busy} onClick={onClose} aria-label={`Close ${title}`}>Close</button></header>
    <div className="devops-setup-dialog-content">{children}</div>
  </dialog>;
}
