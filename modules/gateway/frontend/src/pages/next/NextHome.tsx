/**
 * NextHome — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * The landing page of the opt-in new experience. This story delivers the shell
 * only, so the page's honest content is: what this preview is, that the current
 * UI is still the default, and where each not-yet-migrated capability actually
 * works today.
 *
 * Deliberately NOT here: the Use ADP / Administration navigation and the grouped
 * journey pages. Those are NUI-02 (#5080) onward. Rendering placeholder nav for
 * them would violate the coexistence rule against presenting nonfunctional
 * controls as available features.
 */

import { CurrentUiLinks } from '@/components/next/CurrentUiLinks';

export default function NextHome() {
  return (
    <div className="space-y-8" data-testid="next-home">
      <header>
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white">
          Welcome to the new UI preview
        </h2>
        <p className="mt-2 text-gray-600 dark:text-gray-400 max-w-3xl">
          This is an early look at a navigation built around two journeys: using
          ADP and administering it. It shares your sign-in, your organization and
          the same data as the current UI — anything you change in one is
          effective in the other.
        </p>
        <p className="mt-2 text-gray-600 dark:text-gray-400 max-w-3xl">
          The current UI remains the default and keeps working at all its usual
          addresses. Use <strong>Back to current UI</strong> above to return at
          any time; your bookmarks are unaffected.
        </p>
      </header>

      <section aria-labelledby="next-home-not-migrated">
        <h2
          id="next-home-not-migrated"
          className="text-lg font-semibold text-gray-900 dark:text-white mb-1"
        >
          Where to find everything else
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4 max-w-3xl">
          These capabilities have not moved into the preview yet. Each link opens
          the page that works today in the current UI.
        </p>
        <CurrentUiLinks />
      </section>
    </div>
  );
}
