/**
 * NextLayout — Issue #5079 (NUI-01), extended for #5080 (NUI-02) of EPIC #5078.
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
 *   the header of every /next page. It is a DIFFERENT control from the Use ADP /
 *   Administration journey switch added by #5080 below, and the two are rendered
 *   together: the switch moves between journeys inside the preview, the return
 *   link leaves the preview altogether.
 *
 * The return link is a `Link`, i.e. a client-side navigation, so returning to
 * the current UI keeps the session and the query cache warm. The escape hatch
 * for a broken /next chunk is a separate hard `<a href="/">` in
 * `NextUnavailable` — that case cannot rely on this layout having rendered.
 *
 * ## What #5080 adds
 *
 * The two journeys, as navigation chrome around the same `Outlet`:
 *
 * - A journey switch and a per-journey nav list, both driven by the gated model in
 *   `components/next/journeys.ts`. No predicate lives in this file.
 * - **A per-journey return location.** The current path is recorded as the active
 *   journey's location on every navigation, so switching away and back returns you
 *   where you were rather than to the journey home. The record is scoped to the
 *   active user and organization and validated against live permissions before use
 *   (`useJourneys`), so it cannot leak a prior tenant's position or restore a page
 *   a revoked role no longer permits.
 * - **Administration is withheld, not disabled.** An actor with no administration
 *   entries sees no Administration control and, if they reach `/next/admin` by URL,
 *   is redirected to the Use ADP home. A visible-but-dead tab would be exactly the
 *   misleading control the criteria forbid.
 * - **Focus moves to the page heading on journey change**, so a keyboard or
 *   screen-reader user is not left at the top of the document re-reading the header
 *   after switching journeys.
 * - **A mobile drawer** for the nav, since the sidebar is hidden below `lg`. It
 *   closes on navigation and on Escape, and its trigger is hidden from desktop.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, Navigate, Outlet, useLocation } from 'react-router-dom';
import { WorkspaceSelector } from '@/components/WorkspaceSelector';
import { useAuth } from '@/hooks/useAuth';
import { useJourneys } from '@/hooks/useJourneys';
import { JourneySwitch } from '@/components/next/JourneySwitch';
import { NextNav } from '@/components/next/NextNav';
import { JOURNEY_HOME, journeyForPath } from '@/components/next/journeys';

/** Path the "Back to current UI" control returns to. */
export const CURRENT_UI_HOME = '/';

export function NextLayout() {
  const { user } = useAuth();
  const { pathname } = useLocation();
  const {
    journeys,
    available,
    canAdminister,
    destinationFor,
    rememberLocation,
    activeEntryId,
  } = useJourneys();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const headingRef = useRef<HTMLDivElement>(null);

  // Which journey the current URL belongs to. Every /next path maps to one, so the
  // fallback only covers a path outside the subtree, which cannot render here.
  const activeJourney = journeyForPath(pathname) ?? 'use';

  // An actor who may not administer must not be held on an administration URL.
  const mustLeaveAdmin = activeJourney === 'admin' && !canAdminister;

  // Record where the actor is, per journey, so the switch can return them here.
  // Effect rather than a click handler so a deep link or a browser Back is
  // remembered too, not only a click on our own nav.
  useEffect(() => {
    if (mustLeaveAdmin) return;
    rememberLocation(activeJourney, pathname);
  }, [activeJourney, pathname, rememberLocation, mustLeaveAdmin]);

  // Close the drawer whenever the route changes, including on Back — a drawer left
  // open over a new page hides the content the user asked for.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  // Move focus to the page heading on journey change so the next Tab starts in the
  // new journey rather than back at the document top.
  //
  // Skipped on the FIRST run (#5123). On a fresh load nobody has changed journey, so
  // taking focus into the content only steals it: the skip link and the "Back to
  // current UI" control sit before the heading, so the first Tab landed past them and
  // reaching the navigation meant tabbing backwards. A ref rather than state because
  // this is bookkeeping the render output does not depend on — state here would add a
  // render on every journey change.
  const hasMountedRef = useRef(false);
  useEffect(() => {
    if (!hasMountedRef.current) {
      hasMountedRef.current = true;
      return;
    }
    headingRef.current?.focus();
  }, [activeJourney]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawerOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [drawerOpen]);

  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  if (mustLeaveAdmin) {
    return <Navigate to={JOURNEY_HOME.use} replace />;
  }

  const journey = journeys[activeJourney];
  const activeId = activeEntryId(pathname);

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
              {/* Drawer trigger: the nav sidebar is hidden below `lg`, so without
                  this the journey's entries would be unreachable on a phone. */}
              <button
                type="button"
                onClick={() => setDrawerOpen((open) => !open)}
                aria-expanded={drawerOpen}
                aria-controls="next-nav-drawer"
                aria-label={drawerOpen ? 'Close navigation' : 'Open navigation'}
                data-testid="next-nav-toggle"
                className="lg:hidden rounded-md p-2 text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d={drawerOpen ? 'M6 18L18 6M6 6l12 12' : 'M4 6h16M4 12h16M4 18h16'}
                  />
                </svg>
              </button>
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
                  deliberately separate from the journey switch below. */}
              <Link
                to={CURRENT_UI_HOME}
                data-testid="back-to-current-ui"
                className="px-3 py-2 text-sm font-medium text-primary-700 dark:text-primary-200 border border-primary-300 dark:border-primary-700 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-900 transition-colors"
              >
                ← Back to current UI
              </Link>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 pb-2">
            {/* Same component instance the current UI uses, so switching
                organization in either experience is one shared change. */}
            {user && <WorkspaceSelector />}
            {/* Journey switch: moves BETWEEN journeys inside the preview. Not a
                replacement for the return link above. */}
            <div className="hidden sm:block">
              <JourneySwitch
                journeys={available}
                active={activeJourney}
                destination={destinationFor}
              />
            </div>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="lg:grid lg:grid-cols-[16rem_1fr] lg:gap-8">
          {/* Desktop nav. Hidden below `lg`, where the drawer serves instead. */}
          <div className="hidden lg:block">
            <NextNav journey={journey} activeEntryId={activeId} />
          </div>

          <main id="next-main-content">
            {/* Focus target for a journey change. `tabIndex={-1}` makes it
                programmatically focusable without adding a tab stop. */}
            <div ref={headingRef} tabIndex={-1} className="outline-none">
              <p className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                {journey.label}
              </p>
            </div>
            <Outlet />
          </main>
        </div>
      </div>

      {/* Mobile drawer. Rendered after the main content so a screen reader in DOM
          order reaches the page before the navigation, and only mounted when open
          so its links are not focusable while hidden. */}
      {drawerOpen && (
        <>
          <div
            className="fixed inset-0 z-40 bg-black bg-opacity-50 lg:hidden"
            onClick={closeDrawer}
            aria-hidden="true"
          />
          <div
            id="next-nav-drawer"
            data-testid="next-nav-drawer"
            className="fixed inset-y-0 left-0 z-50 w-72 overflow-y-auto bg-white p-4 shadow-xl dark:bg-gray-900 lg:hidden"
          >
            <div className="mb-4 flex items-center justify-between">
              <span className="text-lg font-semibold text-gray-900 dark:text-white">Menu</span>
              <button
                type="button"
                onClick={closeDrawer}
                aria-label="Close navigation"
                data-testid="next-nav-drawer-close"
                className="rounded-md p-2 text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
              >
                <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
            </div>
            {/* The switch is in the drawer too: below `sm` it is hidden in the
                header, so without this a phone user could not change journey. */}
            <div className="mb-4 sm:hidden">
              <JourneySwitch
                journeys={available}
                active={activeJourney}
                destination={destinationFor}
                onNavigate={closeDrawer}
              />
            </div>
            <NextNav journey={journey} activeEntryId={activeId} onNavigate={closeDrawer} />
          </div>
        </>
      )}
    </div>
  );
}
