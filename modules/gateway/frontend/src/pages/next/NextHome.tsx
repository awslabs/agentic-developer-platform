/**
 * NextHome — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * The landing page of the opt-in new experience. This story delivers the shell
 * only, so the page's honest content is: what this preview is, that the current
 * UI is still the default, and where each not-yet-migrated capability actually
 * works today.
 *
 * **#5080 update:** this is now the landing page of the *Use ADP* journey, and the
 * Use ADP / Administration switch and per-journey navigation live in `NextLayout`
 * around it. The page's own content is still orientation plus honest links: this
 * story migrates no capability, so every entry opens the page that works today in
 * the current UI, labelled as such. The cards below are the Use ADP journey's own
 * entries, from the same gated model the navigation uses.
 */

import { useJourneys } from '@/hooks/useJourneys';
import { JourneyEntryCards } from '@/components/next/JourneyEntryCards';

export default function NextHome() {
  const { journeys } = useJourneys();
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
          Your work in ADP
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4 max-w-3xl">
          These capabilities have not moved into the preview yet. Each link opens
          the page that works today in the current UI.
        </p>
        <JourneyEntryCards sections={journeys.use.sections} idPrefix="next-home" />
      </section>
    </div>
  );
}
