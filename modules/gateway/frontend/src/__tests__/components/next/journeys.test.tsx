/**
 * Tests for the two-journey navigation model — Issue #5080.
 *
 * The model is where this story's authorization-shaped risk lives. Two failure
 * modes matter and each has cases below:
 *
 * - **Advertising too much**: a nav entry whose route the server refuses with 403,
 *   or which `FeatureGate` bounces back to "/". That is a misleading link, and the
 *   fix is that every predicate matches `Navigation.tsx` exactly.
 * - **Advertising too little**: hiding a capability from the people who hold its
 *   permission. The design note calls this out for logs specifically ("moving the
 *   global system page must not remove that access") and #4389 for members.
 *
 * These tests therefore assert the gating matrix against the current sidebar's
 * rules rather than against a prettier restatement of them.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import {
  buildJourneys,
  canEnterAdministration,
  journeyEntries,
  journeyForPath,
  isRestorablePath,
  journeyPermissions,
  JOURNEY_HOME,
  type JourneyPermissions,
} from '@/components/next/journeys';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

function features(overrides: Partial<FeatureFlags> = {}): FeatureFlags {
  return { ...ALL_FEATURES_ENABLED, ...overrides };
}

/**
 * Genuinely every flag on.
 *
 * `ALL_FEATURES_ENABLED` does not mean what its name says: `gitlab`,
 * `orchestration_engine`, `budget_spend`, `agent_control` and `new_ui` are
 * deliberately `false` there because that object is also the fail-closed default
 * while `/features` is in flight or erroring. Tests that need the widest possible
 * set of destinations must turn them on explicitly, so derive the set from the
 * object's own keys rather than restating a list that would go stale.
 */
function allFeaturesOn(): FeatureFlags {
  const on = Object.fromEntries(
    Object.keys(ALL_FEATURES_ENABLED).map((key) => [key, true]),
  );
  return on as unknown as FeatureFlags;
}

/** A plain member: no roles, no permissions. */
const MEMBER: Partial<JourneyPermissions> = {};

/** An org admin holding the READ permissions an org admin normally holds. */
const ORG_ADMIN: Partial<JourneyPermissions> = {
  isOrgAdmin: true,
  canViewOrganizations: true,
  canViewBudgets: true,
  canViewRateLimits: true,
  orgId: 'org-1',
};

/** A platform admin holding everything. */
const PLATFORM_ADMIN: Partial<JourneyPermissions> = {
  isPlatformAdmin: true,
  canViewOrganizations: true,
  canViewBudgets: true,
  canViewRateLimits: true,
  canViewLogs: true,
  canViewPool: true,
  canViewMetrics: true,
  orgId: 'org-1',
};

/** Every entry id in a journey, in nav order. */
function ids(journey: { sections: { entries: { id: string }[] }[] }): string[] {
  return journey.sections.flatMap((s) => s.entries.map((e) => e.id));
}

function useIds(flags: Partial<FeatureFlags> = {}, perms: Partial<JourneyPermissions> = {}) {
  return ids(buildJourneys(features(flags), perms).use);
}

function adminIds(flags: Partial<FeatureFlags> = {}, perms: Partial<JourneyPermissions> = {}) {
  return ids(buildJourneys(features(flags), perms).admin);
}

describe('journey model — Issue #5080', () => {
  describe('Use ADP is everyone’s journey', () => {
    it('gives a member with no permissions a usable Use ADP journey', () => {
      // The whole journey is scoped to the caller server-side, so nothing here is
      // permission-gated. A member seeing an empty journey would be the bug.
      const journeys = buildJourneys(features(), MEMBER);
      expect(journeys.use.sections.length).toBeGreaterThan(0);
      expect(ids(journeys.use)).toContain('runs');
      expect(ids(journeys.use)).toContain('activity');
      expect(ids(journeys.use)).toContain('cli-setup');
    });

    it('is identical for a member and a platform admin', () => {
      // "An administrator starts with the same user experience as a member." The
      // journeys differ by what Administration adds, not by Use ADP shrinking.
      expect(useIds({}, PLATFORM_ADMIN)).toEqual(useIds({}, MEMBER));
    });

    it.each([
      ['chat', 'chats'],
      ['chat', 'chats-new'],
      ['orchestration_engine', 'flows'],
      ['knowledge', 'knowledge'],
      ['budget_spend', 'my-spend'],
      ['connections', 'connections'],
      ['credentials', 'credentials'],
      ['gitlab', 'gitlab'],
    ] as const)('drops %s entries when the feature is off', (flag, entryId) => {
      expect(useIds({ [flag]: true })).toContain(entryId);
      expect(useIds({ [flag]: false })).not.toContain(entryId);
    });

    it('offers both chat pages: one to start a conversation, one to read past ones', () => {
      // #5123: only /my-chats was carried across, so the preview had no way to
      // START a chat — and /my-chats contains no link to one, so the label that
      // claimed otherwise led to a dead end.
      const journey = buildJourneys(features({ chat: true }), MEMBER).use;
      const entries = journeyEntries(journey);

      const startChat = entries.find((e) => e.to === '/chat');
      expect(startChat).toBeDefined();
      expect(startChat?.currentUi).toBe(true);

      const history = entries.find((e) => e.to === '/my-chats');
      expect(history).toBeDefined();
      // The description must not promise starting a chat here.
      expect(history?.description.toLowerCase()).not.toContain('starting a new chat');
    });

    it('gates chat on the feature alone, exactly as the sidebar does', () => {
      // Navigation.tsx pushes /chat under `features.chat` with NO role or
      // permission predicate. Gating it more tightly here would silently take
      // chat away from a plain member who has it today.
      expect(useIds({ chat: true }, MEMBER)).toContain('chats-new');
      // And it is not something only admins reach.
      expect(useIds({ chat: true }, MEMBER)).toEqual(useIds({ chat: true }, PLATFORM_ADMIN));
    });

    it('shows Delivery flows and My spend to a member with no permissions', () => {
      // #4389: a MEMBER resolves to no permissions on the ID-token path, so
      // permission-gating these would hide them from their intended users. The
      // sidebar deliberately gates them on the feature only.
      const entries = useIds({ orchestration_engine: true, budget_spend: true }, MEMBER);
      expect(entries).toContain('flows');
      expect(entries).toContain('my-spend');
    });

    it('offers personal model access wherever it works today, not as a stub', () => {
      const journey = buildJourneys(features({ credentials: true }), MEMBER).use;
      const modelAccess = journeyEntries(journey).find((e) => e.id === 'model-access-personal');
      expect(modelAccess).toBeDefined();
      // It is not built in the preview yet, so it must point at the current-UI
      // page where the personal Bedrock selector actually lives.
      expect(modelAccess?.currentUi).toBe(true);
      expect(modelAccess?.to).toBe('/settings/credentials');
    });

    it('marks the server-owned GitLab path as external, and nothing else', () => {
      // #5123: /gitlab/ is served by the backend and has no react-router route, so
      // it must be flagged for the renderers to emit a real anchor. Asserting the
      // negative too, so a future entry cannot be flagged external by accident and
      // start hard-reloading the SPA.
      const journeys = buildJourneys(allFeaturesOn(), PLATFORM_ADMIN);
      const all = [...journeyEntries(journeys.use), ...journeyEntries(journeys.admin)];
      const external = all.filter((e) => e.external).map((e) => e.to);
      expect(external).toEqual(['/gitlab/']);
    });

    it('never marks a Use ADP entry as platform-scoped', () => {
      // "Use ADP → Model access always means 'for me'." Nothing personal is
      // platform-wide.
      for (const entry of journeyEntries(buildJourneys(features(), PLATFORM_ADMIN).use)) {
        expect(entry.scope).toBeUndefined();
      }
    });
  });

  describe('Administration is a scope, not a competing menu tree', () => {
    it('is not offered to a plain member', () => {
      const journeys = buildJourneys(features(), MEMBER);
      expect(canEnterAdministration(journeys)).toBe(false);
      expect(adminIds({}, MEMBER)).toEqual([]);
    });

    it('is offered to an org admin', () => {
      expect(canEnterAdministration(buildJourneys(features(), ORG_ADMIN))).toBe(true);
    });

    it('is offered to a platform admin', () => {
      expect(canEnterAdministration(buildJourneys(features(), PLATFORM_ADMIN))).toBe(true);
    });

    it('gives org and platform admins one shared journey, differing only in scope', () => {
      // Not two hierarchies: the same journey, with the platform admin reaching
      // more targets inside it.
      const org = buildJourneys(features(), ORG_ADMIN).admin;
      const platform = buildJourneys(features(), PLATFORM_ADMIN).admin;
      expect(org.label).toBe(platform.label);
      expect(org.home).toBe(platform.home);
      // Both reach the shared organization management entry.
      expect(ids(org)).toContain('organizations');
      expect(ids(platform)).toContain('organizations');
    });
  });

  describe('administration gating mirrors the current sidebar exactly', () => {
    it('hides Budgets from an admin lacking BUDGET_READ', () => {
      expect(adminIds({}, { ...ORG_ADMIN, canViewBudgets: false })).not.toContain('budgets');
    });

    it('hides Rate limits from an admin lacking RATELIMIT_READ', () => {
      expect(adminIds({}, { ...ORG_ADMIN, canViewRateLimits: false })).not.toContain('ratelimits');
    });

    it('gates Budgets and Rate limits independently', () => {
      const entries = adminIds({}, { ...PLATFORM_ADMIN, canViewRateLimits: false });
      expect(entries).toContain('budgets');
      expect(entries).not.toContain('ratelimits');
    });

    it('does not let permissions alone substitute for an admin role', () => {
      // The sidebar predicate is AND: a member holding BUDGET_READ reaches the
      // admin CRUD page no more than the sidebar would let them.
      const entries = adminIds({}, { canViewBudgets: true, canViewRateLimits: true });
      expect(entries).not.toContain('budgets');
      expect(entries).not.toContain('ratelimits');
    });

    it('shows a platform admin the budget entries even though isOrgAdmin is false', () => {
      // `hasRole` is exact equality, so a platform admin does NOT satisfy
      // isOrgAdmin. Writing the predicate as isOrgAdmin-only would hide budgets
      // from the platform admin entirely.
      const entries = adminIds({}, { isPlatformAdmin: true, canViewBudgets: true });
      expect(entries).toContain('budgets');
    });

    it.each([
      ['system-org-usage', 'canViewOrganizations'],
      ['system-pool', 'canViewPool'],
      ['system-metrics', 'canViewMetrics'],
    ] as const)('gates %s on its own permission', (entryId, permission) => {
      expect(adminIds({}, PLATFORM_ADMIN)).toContain(entryId);
      expect(adminIds({}, { ...PLATFORM_ADMIN, [permission]: false })).not.toContain(entryId);
    });

    it('hides every system-dashboard entry when the feature is off', () => {
      const entries = adminIds({ system_dashboard: false }, PLATFORM_ADMIN);
      expect(entries).not.toContain('system-health');
      expect(entries).not.toContain('system-pool');
    });

    it('keeps system health platform-admin-only', () => {
      expect(adminIds({}, ORG_ADMIN)).not.toContain('system-health');
    });

    it('keeps GitHub organization links platform-admin-only', () => {
      expect(adminIds({}, PLATFORM_ADMIN)).toContain('tenant-links');
      expect(adminIds({}, ORG_ADMIN)).not.toContain('tenant-links');
    });

    it('keeps administration model access platform-admin-only for now', () => {
      // The design note is explicit that /admin/bedrock-routing/* still requires
      // platform admin, and that revealing it to org admins before the backend
      // extension "would produce 403 responses or expose platform-wide
      // inventory". So this entry must not appear for an org admin yet.
      expect(adminIds({}, PLATFORM_ADMIN)).toContain('model-access-admin');
      expect(adminIds({}, ORG_ADMIN)).not.toContain('model-access-admin');
    });

    it('shows agent definitions to an org admin, as the sidebar does', () => {
      expect(adminIds({}, ORG_ADMIN)).toContain('agents');
    });

    it('shows Organizations & teams on the permission alone', () => {
      // The route has no AdminGuard: an org admin manages their own org here.
      expect(adminIds({}, { canViewOrganizations: true, isOrgAdmin: true })).toContain(
        'organizations',
      );
    });

    it('omits the scoped dashboards when the session has no org', () => {
      // A role without the matching claim would build /org/undefined.
      expect(adminIds({}, { isOrgAdmin: true, orgId: undefined })).not.toContain(
        'my-organization',
      );
      expect(
        adminIds({}, { isDeptAdmin: true, orgId: 'org-1', deptId: undefined }),
      ).not.toContain('my-department');
    });

    it('builds the scoped dashboard paths from the session claims', () => {
      const entries = journeyEntries(
        buildJourneys(features(), { isDeptAdmin: true, orgId: 'org-7', deptId: 'dept-3' })
          .admin,
      );
      expect(entries.find((e) => e.id === 'my-department')?.to).toBe(
        '/org/org-7/department/dept-3',
      );
    });
  });

  describe('a permitted non-admin log viewer keeps their access', () => {
    it('reaches Logs through Administration without any admin role', () => {
      // The design note requires that regrouping the system pages must not remove
      // access for permitted log viewers. A DEPT_ADMIN with LOGS_READ is the case.
      const journeys = buildJourneys(features({ logs: true }), {
        isDeptAdmin: true,
        canViewLogs: true,
      });
      expect(ids(journeys.admin)).toContain('logs');
      // ...and the journey is therefore offered to them, because entry into
      // Administration is keyed off surviving entries rather than a role.
      expect(canEnterAdministration(journeys)).toBe(true);
    });

    it('hides Logs when the permission is absent', () => {
      expect(adminIds({ logs: true }, { ...PLATFORM_ADMIN, canViewLogs: false })).not.toContain(
        'logs',
      );
    });

    it('hides Logs when the feature is off', () => {
      expect(adminIds({ logs: false }, PLATFORM_ADMIN)).not.toContain('logs');
    });
  });

  describe('labels and selector values never confer authority', () => {
    it('labels platform-wide entries with their platform scope', () => {
      // System-wide screens must identify their scope rather than appearing to be
      // filtered by the selected organization.
      const entries = journeyEntries(buildJourneys(features(), PLATFORM_ADMIN).admin);
      expect(entries.find((e) => e.id === 'system-health')?.scope).toBe('platform');
      expect(entries.find((e) => e.id === 'system-metrics')?.scope).toBe('platform');
    });

    it('does not mark organization-scoped entries as platform-wide', () => {
      const entries = journeyEntries(buildJourneys(features(), ORG_ADMIN).admin);
      expect(entries.find((e) => e.id === 'budgets')?.scope).toBeUndefined();
      expect(entries.find((e) => e.id === 'my-organization')?.scope).toBeUndefined();
    });

    it('does not grant an entry merely because an orgId is present', () => {
      // A selected organization is context, not authority: passing an orgId
      // without the role must not produce administration entries.
      expect(canEnterAdministration(buildJourneys(features(), { orgId: 'org-1' }))).toBe(
        false,
      );
    });
  });

  describe('every entry is honest about where it goes', () => {
    it('marks every capability entry as opening the current UI', () => {
      // This story migrates no capability, so nothing may present itself as a
      // working preview page.
      const journeys = buildJourneys(features(), PLATFORM_ADMIN);
      const all = [...journeyEntries(journeys.use), ...journeyEntries(journeys.admin)];
      expect(all.length).toBeGreaterThan(0);
      for (const entry of all) {
        expect(entry.currentUi).toBe(true);
        expect(entry.to.startsWith('/next')).toBe(false);
        expect(entry.description).not.toBe('');
        expect(entry.label).not.toBe('');
      }
    });

    it('carries across every destination the current sidebar offers', () => {
      // The class-level guard (#5123). The matrix above asserts gating
      // CONDITIONS; nothing asserted COVERAGE, which is why a 2053-test suite did
      // not notice that /chat had been dropped entirely. Reading the sidebar's
      // source keeps this honest as that file changes: add an entry there and this
      // fails until the journey model accounts for it.
      const navSource = readFileSync(
        resolve(__dirname, '../../../components/Navigation.tsx'),
        'utf8',
      );
      const navPaths = [...navSource.matchAll(/to: '([^']+)'/g)].map((m) => m[1]);
      // Sanity-check the scrape itself: a regex that silently matched nothing
      // would make this test vacuously green.
      expect(navPaths.length).toBeGreaterThan(15);

      // The widest possible set of destinations. Note this needs BOTH admin roles,
      // not just the platform one: `hasRole` is exact equality, so a platform admin
      // does not satisfy `isOrgAdmin`, and /agents is an org-admin entry in the
      // sidebar and in the model alike. A platform-admin-only fixture here would
      // report /agents as missing when it is present and correctly gated.
      const everyone = { ...PLATFORM_ADMIN, ...ORG_ADMIN, isPlatformAdmin: true };
      const journeys = buildJourneys(allFeaturesOn(), everyone);
      const modelPaths = new Set(
        [...journeyEntries(journeys.use), ...journeyEntries(journeys.admin)].map((e) => e.to),
      );

      // A fragment is a location within a page, not a separate destination: the
      // sidebar's /admin/system#pool and the model's /admin/system are the same
      // page. Compare on the path.
      const missing = navPaths
        .map((p) => p.split('#')[0])
        .filter((p) => !modelPaths.has(p));
      expect(missing).toEqual([]);
    });

    it('gives every entry a unique id within its journey', () => {
      // Two entries may share a destination while a capability still lives inside
      // another page, so identity is the id, and it must be unique for React keys
      // and for active-state matching.
      const journeys = buildJourneys(features(), PLATFORM_ADMIN);
      for (const journey of [journeys.use, journeys.admin]) {
        const entryIds = ids(journey);
        expect(new Set(entryIds).size).toBe(entryIds.length);
      }
    });

    it('emits no empty sections', () => {
      // An empty heading advertises a group with nothing behind it.
      const journeys = buildJourneys(features(), MEMBER);
      for (const journey of [journeys.use, journeys.admin]) {
        for (const section of journey.sections) {
          expect(section.entries.length).toBeGreaterThan(0);
        }
      }
    });
  });

  describe('journeyPermissions defaults deny', () => {
    it('treats every unstated permission as absent', () => {
      const perms = journeyPermissions();
      expect(perms.isPlatformAdmin).toBe(false);
      expect(perms.canViewBudgets).toBe(false);
      expect(perms.canViewLogs).toBe(false);
    });

    it('preserves what the caller states', () => {
      expect(journeyPermissions({ isOrgAdmin: true }).isOrgAdmin).toBe(true);
    });
  });

  describe('journeyForPath', () => {
    it.each([
      ['/next', 'use'],
      ['/next/admin', 'admin'],
      ['/next/admin/anything', 'admin'],
      ['/next/something', 'use'],
    ] as const)('maps %s to the %s journey', (path, expected) => {
      expect(journeyForPath(path)).toBe(expected);
    });

    it.each(['/', '/runs', '/budgets', '/nextish'])(
      'returns null for the current-UI path %s',
      (path) => {
        // A current-UI destination is not a position inside the preview, which is
        // what stops one being restored as a remembered journey location.
        expect(journeyForPath(path)).toBeNull();
      },
    );
  });

  describe('isRestorablePath validates against live permissions', () => {
    it('accepts each journey home for an actor who may enter it', () => {
      const journeys = buildJourneys(features(), PLATFORM_ADMIN);
      expect(isRestorablePath(JOURNEY_HOME.use, journeys)).toBe(true);
      expect(isRestorablePath(JOURNEY_HOME.admin, journeys)).toBe(true);
    });

    it('refuses an administration location once the actor may no longer enter it', () => {
      // The revoked-membership case: a location stored while the user was an admin
      // must not be restorable after the role is gone.
      const journeys = buildJourneys(features(), MEMBER);
      expect(isRestorablePath(JOURNEY_HOME.admin, journeys)).toBe(false);
      expect(isRestorablePath(JOURNEY_HOME.use, journeys)).toBe(true);
    });

    it('refuses a current-UI path', () => {
      expect(isRestorablePath('/runs', buildJourneys(features(), PLATFORM_ADMIN))).toBe(false);
    });

    it('refuses an unknown preview path', () => {
      // Only pages this story actually ships may be restored; an unknown /next
      // path would restore into the scoped 404.
      expect(
        isRestorablePath('/next/not-a-page', buildJourneys(features(), PLATFORM_ADMIN)),
      ).toBe(false);
    });
  });
});
