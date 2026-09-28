/**
 * NextAdminHome — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * The landing page of the Administration journey. Like the Use ADP home it is an
 * orientation page, not a console: this story groups the existing administration
 * pages into a journey and migrates none of them, so every capability here is a
 * labelled link to the page that works today.
 *
 * Two things this page must say out loud, because they are acceptance criteria:
 *
 * 1. **Which scope the actor is administering.** An org admin and a platform admin
 *    share this journey, and the difference is the scope, not the menu. Saying
 *    "your organization" versus "the whole platform" is what stops an org admin
 *    reading a platform-wide number as their own, and it is why System health and
 *    the other platform-wide entries are labelled individually in the nav too.
 * 2. **That the label is not the authority.** The server authorizes every action
 *    behind these links. This page shows what the actor may *navigate to*; it does
 *    not widen what they may do, and an administration label on a screen does not
 *    mean the backend will accept a write there.
 *
 * The route guard for "may this actor be here at all" is in `NextLayout`, which
 * redirects to the Use ADP home when no administration entry survives gating. This
 * page therefore renders only for actors with something in the journey, and does
 * not repeat the check.
 */

import { useAuth } from '@/hooks/useAuth';
import { usePermissions } from '@/hooks/usePermissions';
import { useJourneys } from '@/hooks/useJourneys';
import { JourneyEntryCards } from '@/components/next/JourneyEntryCards';

export default function NextAdminHome() {
  const { user } = useAuth();
  const { isPlatformAdmin } = usePermissions();
  const { journeys } = useJourneys();
  const isPlatform = isPlatformAdmin();

  return (
    <div className="space-y-8" data-testid="next-admin-home">
      <header>
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white">Administration</h2>
        <p className="mt-2 max-w-3xl text-gray-600 dark:text-gray-400">
          Organizations and teams, budgets and limits, model access and system
          health. This is the same sign-in and the same data as the current UI.
        </p>
        {/* The scope statement. Explicit rather than implied by which entries are
            visible, so a platform-wide figure is never read as one organization's. */}
        <p
          className="mt-3 rounded-lg bg-gray-100 p-3 text-sm text-gray-700 dark:bg-gray-800 dark:text-gray-300"
          data-testid="next-admin-scope"
        >
          {isPlatform ? (
            <>
              You are administering <strong>the whole platform</strong>. Entries
              marked <em>Platform-wide</em> cover every organization, not only the
              one selected above.
            </>
          ) : (
            <>
              You are administering <strong>your own organization</strong>
              {user?.orgId ? <> ({user.orgId})</> : null}. Platform-wide settings
              are not part of this journey.
            </>
          )}{' '}
          What you can change is decided by the server, not by this page.
        </p>
      </header>

      <section aria-labelledby="next-admin-where">
        <h3
          id="next-admin-where"
          className="mb-1 text-lg font-semibold text-gray-900 dark:text-white"
        >
          Where each administration task lives
        </h3>
        <p className="mb-4 max-w-3xl text-sm text-gray-600 dark:text-gray-400">
          These pages have not moved into the preview yet. Each link opens the page
          that works today in the current UI.
        </p>

        <JourneyEntryCards sections={journeys.admin.sections} idPrefix="next-admin" />
      </section>
    </div>
  );
}
