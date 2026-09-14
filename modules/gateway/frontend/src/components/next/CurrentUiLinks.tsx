/**
 * CurrentUiLinks — Issue #5079 (NUI-01), rebased onto the journey model in #5080.
 *
 * The new /next experience starts with none of its pages migrated. The
 * coexistence contract forbids rendering nonfunctional controls as if they were
 * available features, so instead of stub screens the new shell links to the
 * working current-UI page for each capability.
 *
 * Two rules make these links truthful, and both are now enforced in one place:
 *
 * 1. Every entry carries the SAME feature gate as its current-UI nav entry, so a
 *    module disabled for this deployment is not advertised here either. Sending a
 *    user to a route that `FeatureGate` bounces back to "/" would be a broken
 *    promise, not a fallback.
 * 2. Administration entries carry the same role AND permission predicates the
 *    current sidebar uses. Checking only the role, as an early revision did, showed
 *    an org admin without BUDGET_READ a link the server would refuse. These are
 *    display hints only — the server is the authorization boundary on every route
 *    behind them — but a hint that disagrees with the sidebar is a misleading link.
 *
 * **#5080 change:** the gating table itself moved to `journeys.ts`, which is now the
 * single source of truth shared with the navigation, the journey switch and the
 * journey home pages. This component keeps its exported shape and behaviour and
 * derives its groups from that model, so there is no longer a second copy of "who
 * sees Budgets" to keep in agreement by hand. The `Use ADP` / `Administration`
 * grouping is unchanged, as is the "Opens in the current UI" label on every entry.
 *
 * These are `Link`s, not anchors: staying inside the SPA is what keeps identity,
 * the active org/workspace and the query cache shared between the two UIs. No
 * second login, and no tokens in URLs.
 */

import { Link } from 'react-router-dom';
import { useFeatures } from '@/hooks/useFeatures';
import { usePermissions } from '@/hooks/usePermissions';
import { buildJourneys, journeyEntries, type JourneyPermissions } from './journeys';

interface CurrentUiLink {
  to: string;
  label: string;
  description: string;
}

/** Groups shown as separate lists so an admin can tell the journeys apart. */
export interface CurrentUiLinkGroup {
  title: string;
  links: CurrentUiLink[];
}

/**
 * Build the link groups for the capabilities that have not been migrated yet.
 *
 * Exported for direct unit testing of the gating rules without rendering. The
 * `perms` shape stays as #5079 defined it — the two administration permissions
 * plus the two roles — and the remaining journey inputs default to denied, so a
 * caller that knows only about budgets and rate limits keeps working.
 */
export function buildCurrentUiLinkGroups(
  features: ReturnType<typeof useFeatures>,
  perms: Partial<JourneyPermissions> & {
    isPlatformAdmin: boolean;
    isOrgAdmin: boolean;
    canViewBudgets: boolean;
    canViewRateLimits: boolean;
  },
): CurrentUiLinkGroup[] {
  const journeys = buildJourneys(features, perms);

  // Two entries may legitimately share a destination in the journey model (personal
  // Model access lives on the Credentials page today). This flat link list is keyed
  // by destination, so the first entry for a `to` wins and the duplicate is dropped
  // — the model keeps the distinction, this view does not need it.
  const toGroup = (title: string, entries: ReturnType<typeof journeyEntries>) => {
    const seen = new Set<string>();
    const links: CurrentUiLink[] = [];
    for (const entry of entries) {
      if (seen.has(entry.to)) continue;
      seen.add(entry.to);
      links.push({ to: entry.to, label: entry.label, description: entry.description });
    }
    return { title, links };
  };

  const groups: CurrentUiLinkGroup[] = [
    toGroup('Use ADP', journeyEntries(journeys.use)),
  ];

  const administration = toGroup('Administration', journeyEntries(journeys.admin));
  // Only when something survived the predicates — an empty "Administration"
  // heading would advertise a journey with nothing in it.
  if (administration.links.length > 0) {
    groups.push(administration);
  }

  return groups;
}

export function CurrentUiLinks() {
  const features = useFeatures();
  const perms = usePermissions();
  const groups = buildCurrentUiLinkGroups(features, {
    isPlatformAdmin: perms.isPlatformAdmin(),
    isOrgAdmin: perms.isOrgAdmin(),
    canViewBudgets: perms.canViewBudgets(),
    canViewRateLimits: perms.canViewRateLimits(),
  });

  return (
    <div className="space-y-6" data-testid="next-current-ui-links">
      {groups.map((group) => (
        <section key={group.title} aria-labelledby={`next-links-${group.title.replace(/\s+/g, '-')}`}>
          <h3
            id={`next-links-${group.title.replace(/\s+/g, '-')}`}
            className="text-sm font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-3"
          >
            {group.title}
          </h3>
          <ul className="grid gap-3 sm:grid-cols-2">
            {group.links.map((link) => (
              <li key={link.to}>
                <Link
                  to={link.to}
                  className="block h-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 hover:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 transition-colors"
                >
                  <span className="block font-medium text-gray-900 dark:text-white">
                    {link.label}
                  </span>
                  <span className="block text-sm text-gray-600 dark:text-gray-400 mt-1">
                    {link.description}
                  </span>
                  <span className="block text-xs text-gray-500 dark:text-gray-500 mt-2">
                    Opens in the current UI
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
