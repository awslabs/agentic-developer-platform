/**
 * The two-journey navigation model — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * This module is the single source of truth for what appears in the new UI's
 * navigation, in which journey, and under which conditions. It is deliberately
 * PURE: no React, no hooks, no imports from the component tree. Everything that
 * renders navigation — the sidebar, the mobile drawer, the journey switch and the
 * "where to find everything else" list on the home pages — derives from
 * `buildJourneys()` rather than from its own copy of the rules.
 *
 * ## Why one model instead of per-component gating
 *
 * The current UI's `Navigation.tsx` builds a flat list with an inline predicate per
 * entry. #5079 mirrored a handful of those predicates in its own `CurrentUiLinks`
 * component, and that duplication was already the biggest risk in that file: two
 * tables that must agree about who sees Budgets, kept in agreement by hand. Adding
 * a third and fourth copy for the nav and the drawer would guarantee drift, and
 * drift here has a specific bad shape — a nav entry the server will refuse with
 * 403, or a capability that silently disappears for the people who hold its
 * permission.
 *
 * So the predicates live here once. `CurrentUiLinks` was replaced outright rather
 * than left deriving from this model: once both home pages render the journey's own
 * entries via `JourneyEntryCards`, nothing rendered it, and a second unused list
 * component mirroring the same rules is exactly the drift risk this module exists
 * to remove. Its gating assertions live on in `journeys.test.tsx`, against the model
 * itself rather than through a component.
 *
 * ## What this story does and does not change
 *
 * This is step 2 of the design note's delivery sequence: introduce the two modes
 * and *group the existing pages*, "retaining old URLs, feature gates, and existing
 * backend permissions until the organization-admin extension is tested". So no
 * capability moves into the preview here. Every capability entry is a labelled
 * link to the page that works today (`currentUi: true`), because the coexistence
 * contract forbids rendering nonfunctional controls as available features. Only
 * the two journey home pages live inside `/next`.
 *
 * ## Gating rules, and why they are shaped this way
 *
 * Every predicate below mirrors the corresponding entry in `Navigation.tsx`
 * exactly. Three subtleties that are easy to get wrong and are covered by tests:
 *
 * 1. **`hasRole` is exact equality**, not hierarchical (`AuthContext`), so a
 *    PLATFORM_ADMIN does *not* satisfy `isOrgAdmin()`. Anything both roles reach
 *    must be written `isPlatformAdmin || isOrgAdmin`, as the sidebar writes it.
 * 2. **`/flows` and `/budget` are feature-gated but NOT permission-gated**, on
 *    purpose (#4389): a MEMBER resolves to `[USAGE_READ]` (or `[]`) on the
 *    ID-token path, so a client-side permission check would hide these screens
 *    from exactly the people they were built for. The server scopes them to the
 *    caller.
 * 3. **Logs are gated on the permission, not on an admin role.** The design note
 *    is explicit that if permitted log viewers include non-platform admins, moving
 *    the global page into Administration "must not remove that access". This is
 *    why the Administration journey is offered whenever *any* administration entry
 *    survives, rather than being keyed off an admin role — a DEPT_ADMIN with
 *    LOGS_READ keeps reaching their logs.
 *
 * ## Roles are scopes, not separate menu trees
 *
 * There is one navigation with two journeys. An administrator sees the same Use
 * ADP journey a member sees and switches to Administration when they need it;
 * there is no separate org-admin vs. platform-admin hierarchy. Entries that are
 * platform-wide rather than organization-filtered carry `scope: 'platform'` so the
 * UI can say so, per the design's rule that system-wide screens identify their
 * scope.
 *
 * **These predicates are display hints only.** The server is the authorization
 * boundary on every route behind them. A visible label or a selected organization
 * confers no backend authority; the point of matching the sidebar's predicates is
 * to avoid *advertising* something the server would refuse.
 */

import type { FeatureFlags } from '@/services/features';

/** The two journeys. `use` is everyone's default. */
export type JourneyId = 'use' | 'admin';

/** Route of each journey's landing page inside the preview. */
export const JOURNEY_HOME: Record<JourneyId, string> = {
  use: '/next',
  admin: '/next/admin',
};

export interface JourneyEntry {
  /** Stable identity, independent of destination. Two entries may share a `to`
   *  while a capability still lives inside another page today. */
  id: string;
  /** Destination. A current-UI path unless `currentUi` is false. */
  to: string;
  label: string;
  description: string;
  /**
   * True when following this entry leaves the preview for the working current UI.
   * The UI must label these; an unlabelled link out of the preview reads as a
   * migrated page that happens to look different.
   */
  currentUi: boolean;
  /**
   * Present when the destination is platform-wide rather than scoped to the
   * selected organization. Rendered as an explicit scope label so a system-wide
   * screen is never mistaken for an organization-filtered one.
   */
  scope?: 'platform';
}

export interface JourneySection {
  title: string;
  entries: JourneyEntry[];
}

export interface Journey {
  id: JourneyId;
  /** Nav label. */
  label: string;
  /** One line describing the journey, used by the switch and the home pages. */
  description: string;
  home: string;
  sections: JourneySection[];
}

/**
 * The permission inputs, as already-evaluated booleans.
 *
 * Booleans rather than the predicate functions because this module is pure and
 * unit-tested across a role matrix; the calling component evaluates
 * `usePermissions()` once. This mirrors the shape #5079 established for
 * `buildCurrentUiLinkGroups`.
 */
export interface JourneyPermissions {
  isPlatformAdmin: boolean;
  isOrgAdmin: boolean;
  isDeptAdmin: boolean;
  canViewOrganizations: boolean;
  canViewBudgets: boolean;
  canViewRateLimits: boolean;
  canViewLogs: boolean;
  canViewPool: boolean;
  canViewMetrics: boolean;
  /** Active organization from the shared session, for the scoped dashboards. */
  orgId?: string;
  /** Active department from the shared session. */
  deptId?: string;
}

/** Defaults so callers and tests can specify only the dimension under test. */
const NO_PERMISSIONS: JourneyPermissions = {
  isPlatformAdmin: false,
  isOrgAdmin: false,
  isDeptAdmin: false,
  canViewOrganizations: false,
  canViewBudgets: false,
  canViewRateLimits: false,
  canViewLogs: false,
  canViewPool: false,
  canViewMetrics: false,
};

/** Normalise a partial permission set. Anything unstated is denied. */
export function journeyPermissions(
  perms: Partial<JourneyPermissions> = {},
): JourneyPermissions {
  return { ...NO_PERMISSIONS, ...perms };
}

/** Drop sections that gated down to nothing — an empty heading advertises a
 *  capability group with nothing behind it. */
function nonEmpty(sections: JourneySection[]): JourneySection[] {
  return sections.filter((section) => section.entries.length > 0);
}

/**
 * Build the Use ADP journey: a person's own work. Every entry here is scoped to
 * the caller by the server, so this journey is available to everyone including a
 * MEMBER with no permissions at all.
 */
function buildUseAdp(features: FeatureFlags): Journey {
  const work: JourneyEntry[] = [
    {
      id: 'runs',
      to: '/runs',
      label: 'Agent runs',
      description: 'The run dashboard and the detail of each run.',
      currentUi: true,
    },
    {
      id: 'activity',
      to: '/activity',
      label: 'Agent activity',
      description: 'Recent agent activity and invocations.',
      currentUi: true,
    },
  ];

  // Chats are one capability across two current-UI pages; the design groups both
  // under Agent activity.
  if (features.chat) {
    work.push({
      id: 'chats',
      to: '/my-chats',
      label: 'My chats',
      description: 'Conversation history, and starting a new chat.',
      currentUi: true,
    });
  }
  if (features.orchestration_engine) {
    work.push({
      id: 'flows',
      to: '/flows',
      label: 'Delivery flows',
      description: 'Flow inventory and flow details.',
      currentUi: true,
    });
  }

  const setup: JourneyEntry[] = [
    {
      id: 'cli-setup',
      to: '/setup',
      label: 'CLI setup',
      description: 'Set up Codex or Claude Code against ADP.',
      currentUi: true,
    },
  ];

  if (features.connections) {
    setup.push({
      id: 'connections',
      to: '/settings/connections',
      label: 'Connections',
      description: 'GitHub repositories and other connected services.',
      currentUi: true,
    });
  }
  if (features.credentials) {
    setup.push({
      id: 'credentials',
      to: '/settings/credentials',
      label: 'Credentials',
      description: 'Your vault and connected AWS accounts.',
      currentUi: true,
    });
    // The design gives personal model access its own entry, but it is not built
    // yet: today the personal Bedrock selector lives inside the Credentials page.
    // So this entry points at the page where the capability actually works,
    // labelled as such, rather than at a stub. It shares a destination with the
    // entry above and is distinguished by `id`.
    setup.push({
      id: 'model-access-personal',
      to: '/settings/credentials',
      label: 'Model access',
      description:
        'Choose the AWS account used for your own model calls, or inherit the default. On the Credentials page today.',
      currentUi: true,
    });
  }
  if (features.gitlab) {
    setup.push({
      id: 'gitlab',
      to: '/gitlab/',
      label: 'GitLab',
      description: 'Open the connected GitLab instance.',
      currentUi: true,
    });
  }

  const insight: JourneyEntry[] = [];
  if (features.knowledge) {
    insight.push({
      id: 'knowledge',
      to: '/knowledge',
      label: 'Knowledge',
      description: 'Your knowledge sources and their content.',
      currentUi: true,
    });
  }
  // Feature-gated but deliberately NOT permission-gated — see the file comment.
  if (features.budget_spend) {
    insight.push({
      id: 'my-spend',
      to: '/budget',
      label: 'My spend',
      description: 'Your personal usage and allowance. No administration controls.',
      currentUi: true,
    });
  }

  return {
    id: 'use',
    label: 'Use ADP',
    description: 'Your work: agents, connections, knowledge and your own spend.',
    home: JOURNEY_HOME.use,
    sections: nonEmpty([
      { title: 'Your work', entries: work },
      { title: 'Setup & connections', entries: setup },
      { title: 'Knowledge & spend', entries: insight },
    ]),
  };
}

/**
 * Build the Administration journey: running the platform or an organization.
 *
 * Every predicate mirrors `Navigation.tsx`. If this gates down to nothing the
 * journey is not offered at all (see `canEnterAdministration`), which is how a
 * plain member ends up with only Use ADP without Administration being keyed off a
 * role.
 */
function buildAdministration(
  features: FeatureFlags,
  perms: JourneyPermissions,
): Journey {
  // Written as an explicit OR because `hasRole` is exact equality: a platform
  // admin does not satisfy `isOrgAdmin`.
  const isAdmin = perms.isPlatformAdmin || perms.isOrgAdmin;

  const people: JourneyEntry[] = [];
  if (perms.canViewOrganizations) {
    // Permission only, no role gate — an org admin legitimately manages their own
    // organization's structure here, as App.tsx documents for this route.
    people.push({
      id: 'organizations',
      to: '/admin/organizations',
      label: 'Organizations & teams',
      description: 'Organization structure, departments, teams and people.',
      currentUi: true,
    });
  }
  if (perms.isOrgAdmin && perms.orgId) {
    people.push({
      id: 'my-organization',
      to: `/org/${perms.orgId}`,
      label: 'My organization',
      description: "The dashboard for the organization you administer.",
      currentUi: true,
    });
  }
  if (perms.isDeptAdmin && perms.orgId && perms.deptId) {
    people.push({
      id: 'my-department',
      to: `/org/${perms.orgId}/department/${perms.deptId}`,
      label: 'My department',
      description: 'The dashboard for the department you administer.',
      currentUi: true,
    });
  }
  if (isAdmin) {
    people.push({
      id: 'access-requests',
      to: '/admin/access-requests',
      label: 'Access requests',
      description: 'Membership requests awaiting a decision.',
      currentUi: true,
    });
  }
  if (perms.isOrgAdmin) {
    // Role only, no feature gate and no permission — as in the sidebar.
    people.push({
      id: 'agents',
      to: '/agents',
      label: 'Agent definitions',
      description: 'The agent definitions authorized for your organization.',
      currentUi: true,
    });
  }
  if (perms.isPlatformAdmin) {
    people.push({
      id: 'tenant-links',
      to: '/admin/tenant-links',
      label: 'GitHub organization links',
      description: 'Links between GitHub organizations and ADP tenants.',
      currentUi: true,
      scope: 'platform',
    });
  }

  const limits: JourneyEntry[] = [];
  // Both entries gate on their own READ permission AND the admin role, and are
  // gated independently because the permissions are independent.
  if (isAdmin && perms.canViewBudgets) {
    limits.push({
      id: 'budgets',
      to: '/budgets',
      label: 'Budgets',
      description: 'Spending caps by organization, team and person.',
      currentUi: true,
    });
  }
  if (isAdmin && perms.canViewRateLimits) {
    limits.push({
      id: 'ratelimits',
      to: '/ratelimits',
      label: 'Rate limits',
      description: 'Request and token limits.',
      currentUi: true,
    });
  }
  // Administration-side model access is Bedrock account routing, which today
  // lives inside the Budgets page and whose endpoints still require platform
  // admin (the design note's "current backend versus requested design"). The
  // organization-admin extension is a later story, so this entry stays
  // platform-admin-only and is not advertised to org admins.
  if (perms.isPlatformAdmin && perms.canViewBudgets) {
    limits.push({
      id: 'model-access-admin',
      to: '/budgets',
      label: 'Model access',
      description:
        'Bedrock destinations and routing rules. Inside the Budgets page today.',
      currentUi: true,
      scope: 'platform',
    });
  }

  const system: JourneyEntry[] = [];
  if (features.system_dashboard && perms.isPlatformAdmin) {
    system.push({
      id: 'system-health',
      to: '/admin/system',
      label: 'System health',
      description: 'Platform-wide health and usage.',
      currentUi: true,
      scope: 'platform',
    });
    if (perms.canViewOrganizations) {
      system.push({
        id: 'system-org-usage',
        to: '/admin/system#organizations',
        label: 'Organization usage',
        description: 'Usage across all organizations.',
        currentUi: true,
        scope: 'platform',
      });
    }
    if (perms.canViewPool) {
      system.push({
        id: 'system-pool',
        to: '/admin/system#pool',
        label: 'Pool health',
        description: 'Connection pool health.',
        currentUi: true,
        scope: 'platform',
      });
    }
    if (perms.canViewMetrics) {
      system.push({
        id: 'system-metrics',
        to: '/admin/system#metrics',
        label: 'System metrics',
        description: 'Platform-wide metrics.',
        currentUi: true,
        scope: 'platform',
      });
    }
  }
  // Logs keep their own feature + permission checks and NO admin-role gate, so a
  // permitted non-admin viewer does not lose access by this regrouping.
  if (features.logs && perms.canViewLogs) {
    system.push({
      id: 'logs',
      to: '/logs',
      label: 'Logs',
      description: 'The logs you are authorized to read.',
      currentUi: true,
    });
  }
  if (features.indexing && perms.isPlatformAdmin) {
    system.push({
      id: 'indexing',
      to: '/admin/indexing',
      label: 'Indexing status',
      description: 'Repository indexing status.',
      currentUi: true,
      scope: 'platform',
    });
  }

  return {
    id: 'admin',
    label: 'Administration',
    description:
      'Running ADP: organizations and teams, budgets and limits, model access and system health.',
    home: JOURNEY_HOME.admin,
    sections: nonEmpty([
      { title: 'Organizations & teams', entries: people },
      { title: 'Budgets & limits', entries: limits },
      { title: 'System', entries: system },
    ]),
  };
}

/** Both journeys, gated for this actor. Use ADP is always present. */
export function buildJourneys(
  features: FeatureFlags,
  perms: Partial<JourneyPermissions> = {},
): Record<JourneyId, Journey> {
  const resolved = journeyPermissions(perms);
  return {
    use: buildUseAdp(features),
    admin: buildAdministration(features, resolved),
  };
}

/**
 * Whether to offer the Administration journey at all.
 *
 * Keyed off "does any administration entry survive the same predicates the
 * sidebar uses", not off an admin role. That is what keeps a permitted non-admin
 * log viewer's access reachable while a plain member sees only Use ADP.
 */
export function canEnterAdministration(journeys: Record<JourneyId, Journey>): boolean {
  return journeys.admin.sections.length > 0;
}

/** Flatten a journey's entries in nav order. */
export function journeyEntries(journey: Journey): JourneyEntry[] {
  return journey.sections.flatMap((section) => section.entries);
}

/**
 * The journey a path belongs to.
 *
 * Only the preview's own routes have a journey; a current-UI path returns null,
 * which is what stops a remembered current-UI destination from being treated as a
 * position inside the preview.
 */
export function journeyForPath(pathname: string): JourneyId | null {
  if (pathname === JOURNEY_HOME.admin || pathname.startsWith(`${JOURNEY_HOME.admin}/`)) {
    return 'admin';
  }
  if (pathname === JOURNEY_HOME.use || pathname.startsWith(`${JOURNEY_HOME.use}/`)) {
    return 'use';
  }
  return null;
}

/**
 * Whether `pathname` is somewhere this actor may currently go inside the preview.
 *
 * Used to validate a *remembered* location before restoring it. A permission
 * revoked, or a feature disabled, since the location was stored must not be able
 * to send the user back to a page they may no longer see — so this checks the
 * live gated model rather than trusting what was persisted.
 */
export function isRestorablePath(
  pathname: string,
  journeys: Record<JourneyId, Journey>,
): boolean {
  const journey = journeyForPath(pathname);
  if (!journey) return false;
  if (journey === 'admin' && !canEnterAdministration(journeys)) return false;
  // Journey home pages are always restorable; deeper preview routes must still be
  // a page this story actually ships.
  return pathname === journeys[journey].home;
}
