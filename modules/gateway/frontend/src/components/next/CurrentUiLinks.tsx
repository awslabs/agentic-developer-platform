/**
 * CurrentUiLinks — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * The new /next experience starts with none of its pages migrated. The
 * coexistence contract forbids rendering nonfunctional controls as if they were
 * available features, so instead of stub screens the new shell links to the
 * working current-UI page for each capability.
 *
 * Two rules make these links truthful:
 *
 * 1. Every entry carries the SAME feature gate as its current-UI nav entry, so a
 *    module disabled for this deployment is not advertised here either. Sending a
 *    user to a route that `FeatureGate` bounces back to "/" would be a broken
 *    promise, not a fallback.
 * 2. Administration entries carry the same role AND permission predicates the
 *    current sidebar uses — `canViewBudgets() && (isPlatformAdmin() ||
 *    isOrgAdmin())` for Budgets and the `canViewRateLimits()` equivalent for Rate
 *    Limits (`Navigation.tsx`). Checking only the role, as an earlier revision of
 *    this file did, showed an org admin without BUDGET_READ a link the server
 *    would refuse. As in `Navigation`, these are display hints only — the server
 *    is the authorization boundary on every route behind them — but a hint that
 *    disagrees with the sidebar is a misleading link, so the predicates must match
 *    rather than approximate.
 *
 * These are `Link`s, not anchors: staying inside the SPA is what keeps identity,
 * the active org/workspace and the query cache shared between the two UIs. No
 * second login, and no tokens in URLs.
 */

import { Link } from 'react-router-dom';
import { useFeatures } from '@/hooks/useFeatures';
import { usePermissions } from '@/hooks/usePermissions';

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
 * Exported for direct unit testing of the gating rules without rendering.
 */
export function buildCurrentUiLinkGroups(
  features: ReturnType<typeof useFeatures>,
  perms: {
    isPlatformAdmin: boolean;
    isOrgAdmin: boolean;
    canViewBudgets: boolean;
    canViewRateLimits: boolean;
  },
): CurrentUiLinkGroup[] {
  const useAdp: CurrentUiLink[] = [
    { to: '/runs', label: 'Agent runs', description: 'The run dashboard and its details.' },
    { to: '/activity', label: 'Agent activity', description: 'Recent agent activity and invocations.' },
    { to: '/setup', label: 'CLI setup', description: 'Set up Codex or Claude Code against ADP.' },
  ];

  if (features.chat) {
    useAdp.push({ to: '/my-chats', label: 'My chats', description: 'Conversation history and new chats.' });
  }
  if (features.orchestration_engine) {
    useAdp.push({ to: '/flows', label: 'Delivery flows', description: 'Flow inventory and flow details.' });
  }
  if (features.connections) {
    useAdp.push({
      to: '/settings/connections',
      label: 'Connections',
      description: 'GitHub repositories and other connected services.',
    });
  }
  if (features.credentials) {
    useAdp.push({
      to: '/settings/credentials',
      label: 'Credentials',
      description: 'Your vault and connected AWS accounts.',
    });
  }
  if (features.knowledge) {
    useAdp.push({ to: '/knowledge', label: 'Knowledge', description: 'Your knowledge sources and content.' });
  }
  if (features.budget_spend) {
    useAdp.push({ to: '/budget', label: 'My spend', description: 'Your personal usage and allowance.' });
  }

  const groups: CurrentUiLinkGroup[] = [{ title: 'Use ADP', links: useAdp }];

  // Administration entries mirror the current sidebar's predicates exactly:
  // Navigation gates each of these on its own READ permission AND the admin role,
  // so an admin missing that permission is not pointed at a page the server will
  // refuse. Each link is gated independently, because the two permissions are
  // independent — an admin can hold one and not the other.
  const isAdmin = perms.isPlatformAdmin || perms.isOrgAdmin;
  const administration: CurrentUiLink[] = [];

  if (isAdmin && perms.canViewBudgets) {
    administration.push({
      to: '/budgets',
      label: 'Budgets',
      description: 'Spending caps by organization, team and person.',
    });
  }
  if (isAdmin && perms.canViewRateLimits) {
    administration.push({
      to: '/ratelimits',
      label: 'Rate limits',
      description: 'Request and token limits.',
    });
  }

  // Only when something survived the predicates — an empty "Administration"
  // heading would advertise a journey with nothing in it.
  if (administration.length > 0) {
    groups.push({ title: 'Administration', links: administration });
  }

  return groups;
}

export function CurrentUiLinks() {
  const features = useFeatures();
  const { isPlatformAdmin, isOrgAdmin, canViewBudgets, canViewRateLimits } = usePermissions();
  const groups = buildCurrentUiLinkGroups(features, {
    isPlatformAdmin: isPlatformAdmin(),
    isOrgAdmin: isOrgAdmin(),
    canViewBudgets: canViewBudgets(),
    canViewRateLimits: canViewRateLimits(),
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
