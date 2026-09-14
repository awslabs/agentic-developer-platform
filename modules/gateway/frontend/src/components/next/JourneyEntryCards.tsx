/**
 * Card list for a journey's entries — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * Used by both journey home pages. The sidebar (`NextNav`) is a compact list for
 * moving around; these cards are the orientation view, carrying each entry's
 * description so a first-time visitor can tell what a capability is before
 * following it.
 *
 * Shared rather than written twice because the two labels it renders are
 * acceptance criteria, and two copies would let one of them drift:
 *
 * - **"Opens in the current UI"** on every entry that leaves the preview. This
 *   story migrates no capability, so every capability link goes to the working
 *   current-UI page, and an unlabelled one would read as a migrated page.
 * - **"Platform-wide"** on entries not filtered by the selected organization, so a
 *   system-wide screen is never mistaken for an organization-scoped one.
 *
 * These are `Link`s, not anchors, for the reason #5079 documented: staying inside
 * the SPA is what keeps identity, the active organization/workspace and the query
 * cache shared between the two UIs. A raw `<a>` would hard-reload and discard them.
 */

import { Link } from 'react-router-dom';
import type { JourneySection } from './journeys';

interface JourneyEntryCardsProps {
  sections: JourneySection[];
  /** Distinguishes the two pages' generated heading ids. */
  idPrefix: string;
}

export function JourneyEntryCards({ sections, idPrefix }: JourneyEntryCardsProps) {
  return (
    <div className="space-y-6" data-testid="next-entry-cards">
      {sections.map((section) => {
        const headingId = `${idPrefix}-${section.title
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, '-')}`;
        return (
          <section key={section.title} aria-labelledby={headingId}>
            <h3
              id={headingId}
              className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
            >
              {section.title}
            </h3>
            <ul className="grid gap-3 sm:grid-cols-2">
              {section.entries.map((entry) => (
                <li key={entry.id}>
                  <Link
                    to={entry.to}
                    data-testid={`next-entry-card-${entry.id}`}
                    className="block h-full rounded-lg border border-gray-200 bg-white p-4 transition-colors hover:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-700 dark:bg-gray-800"
                  >
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-gray-900 dark:text-white">
                        {entry.label}
                      </span>
                      {entry.scope === 'platform' && (
                        // The scope of the data behind the link, not a claim about
                        // the actor's authority.
                        <span className="rounded bg-gray-200 px-1.5 py-0.5 text-xs font-normal text-gray-700 dark:bg-gray-700 dark:text-gray-200">
                          Platform-wide
                        </span>
                      )}
                    </span>
                    <span className="mt-1 block text-sm text-gray-600 dark:text-gray-400">
                      {entry.description}
                    </span>
                    {entry.currentUi && (
                      <span className="mt-2 block text-xs text-gray-500 dark:text-gray-500">
                        Opens in the current UI
                      </span>
                    )}
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
