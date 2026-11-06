import type { ReactNode } from 'react';

/**
 * One card of the Dev Box page, in the same visual language as Settings → Connections
 * (owner, 2026-10-26: the page did not look like the other settings pages).
 */
export default function Panel({ title, label, description, actions, children, className = '' }: {
  title: ReactNode; label: string; description?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string;
}) {
  return <section aria-label={label}
    className={`rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-gray-900 ${className}`}>
    <div className="flex items-start justify-between gap-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
        {description && <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{description}</p>}
      </div>
      {actions}
    </div>
    {children}
  </section>;
}
