import type { ReactNode } from 'react';

/** Decorative line icon, shared with the platform navigation. */
export function LineIcon({ children, className = 'h-5 w-5 shrink-0' }: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}
