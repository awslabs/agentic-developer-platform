/**
 * NextLayout — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * The isolated layout for the opt-in new experience under /next/*. It is a
 * sibling of `MainLayout`, not a wrapper around it: the current UI's header,
 * sidebar and banners keep rendering exactly as before, and nothing in this file
 * is imported by the current layout.
 *
 * Isolation rules this file follows, because they are acceptance criteria:
 *
 * - **No global CSS and no module-level side effects.** Styling is Tailwind
 *   utilities scoped to this subtree, using the existing `primary-*` theme
 *   tokens. Nothing is added to `index.css` and nothing runs at import time, so
 *   loading this chunk cannot alter the current layout.
 * - **Shared identity and context.** It reads the same `useAuth` state the
 *   current UI reads, and renders the same `WorkspaceSelector`, so the active
 *   organization is one shared value rather than a second copy. There is no
 *   second login and no token in any URL.
 * - **The return link is persistent and outer.** "Back to current UI" appears in
 *   the header of every /next page. NUI-02 will add the Use ADP / Administration
 *   journey switch *inside* this experience; that switch is a different control
 *   and must not replace this one.
 *
 * The return link is a `Link`, i.e. a client-side navigation, so returning to
 * the current UI keeps the session and the query cache warm. The escape hatch
 * for a broken /next chunk is a separate hard `<a href="/">` in
 * `NextUnavailable` — that case cannot rely on this layout having rendered.
 */

import { Link, Outlet } from 'react-router-dom';
import { WorkspaceSelector } from '@/components/WorkspaceSelector';
import { useAuth } from '@/hooks/useAuth';

/** Path the "Back to current UI" control returns to. */
export const CURRENT_UI_HOME = '/';

export function NextLayout() {
  const { user } = useAuth();

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900" data-testid="next-layout">
      <a
        href="#next-main-content"
        className="skip-link focus:absolute focus:top-0 focus:left-0 focus:z-50 focus:p-4 focus:bg-primary-600 focus:text-white focus:opacity-100"
      >
        Skip to main content
      </a>

      <header className="bg-white dark:bg-gray-800 shadow-sm sticky top-0 z-30">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex flex-wrap items-center justify-between gap-3 py-3">
            <div className="flex items-center gap-3">
              <h1 className="text-xl font-bold text-gray-900 dark:text-white">
                Agentic Developer Platform
              </h1>
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800 dark:bg-primary-900 dark:text-primary-100">
                New UI preview
              </span>
            </div>

            <div className="flex items-center gap-4">
              {user && (
                <span className="text-sm text-gray-600 dark:text-gray-400">
                  {user.githubLogin || user.name || user.email || user.id}
                </span>
              )}
              {/* Persistent return link — present on every /next page, and
                  deliberately separate from the journey switch NUI-02 adds. */}
              <Link
                to={CURRENT_UI_HOME}
                data-testid="back-to-current-ui"
                className="px-3 py-2 text-sm font-medium text-primary-700 dark:text-primary-200 border border-primary-300 dark:border-primary-700 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-900 transition-colors"
              >
                ← Back to current UI
              </Link>
            </div>
          </div>
          {/* Same component instance the current UI uses, so switching
              organization in either experience is one shared change. */}
          {user && <WorkspaceSelector />}
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <main id="next-main-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
