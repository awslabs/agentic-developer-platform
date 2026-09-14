/**
 * Tests for CurrentUiLinks — Issue #5079.
 *
 * The acceptance criterion is that capabilities not yet migrated have clear links
 * to *working* current-UI pages. "Working" is the part that can silently break:
 * a link to a feature-gated route that FeatureGate bounces back to "/" looks like
 * a fallback and behaves like a dead end.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import {
  CurrentUiLinks,
  buildCurrentUiLinkGroups,
} from '@/components/next/CurrentUiLinks';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

const mockUseFeatures = vi.fn<() => FeatureFlags>();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
}));

const mockUsePermissions = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockUsePermissions(),
}));

function features(overrides: Partial<FeatureFlags> = {}): FeatureFlags {
  return { ...ALL_FEATURES_ENABLED, ...overrides };
}

interface TestPerms {
  isPlatformAdmin?: boolean;
  isOrgAdmin?: boolean;
  canViewBudgets?: boolean;
  canViewRateLimits?: boolean;
}

function renderLinks(flags: Partial<FeatureFlags> = {}, perms: TestPerms = {}) {
  mockUseFeatures.mockReturnValue(features(flags));
  mockUsePermissions.mockReturnValue({
    isPlatformAdmin: () => perms.isPlatformAdmin ?? false,
    isOrgAdmin: () => perms.isOrgAdmin ?? false,
    // Default TRUE so the existing role-focused cases below keep asserting the
    // role dimension: an admin normally holds these READ permissions, and the
    // cases that probe a missing one set it explicitly.
    canViewBudgets: () => perms.canViewBudgets ?? true,
    canViewRateLimits: () => perms.canViewRateLimits ?? true,
  });
  return render(
    <MemoryRouter>
      <CurrentUiLinks />
    </MemoryRouter>,
  );
}

/** Collect the hrefs the component actually rendered. */
function renderedHrefs(): string[] {
  return screen
    .getAllByRole('link')
    .map((a) => a.getAttribute('href') ?? '')
    .filter(Boolean);
}

describe('CurrentUiLinks — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('always offers the ungated current-UI pages', () => {
    // These three carry no feature gate in App.tsx, so they are safe to advertise
    // unconditionally.
    renderLinks();
    const hrefs = renderedHrefs();
    expect(hrefs).toContain('/runs');
    expect(hrefs).toContain('/activity');
    expect(hrefs).toContain('/setup');
  });

  it('labels every link as opening the current UI', () => {
    renderLinks();
    // One label per link, so no entry can be mistaken for a migrated page.
    expect(screen.getAllByText('Opens in the current UI')).toHaveLength(
      renderedHrefs().length,
    );
  });

  describe('feature gating matches the routes being linked', () => {
    it.each([
      ['chat', '/my-chats'],
      ['connections', '/settings/connections'],
      ['credentials', '/settings/credentials'],
      ['knowledge', '/knowledge'],
      ['budget_spend', '/budget'],
      ['orchestration_engine', '/flows'],
    ] as [keyof FeatureFlags, string][])(
      'links %s -> %s only when the flag is on',
      (flag, href) => {
        // Unmount between the two renders: testing-library appends each render to
        // the same document, so leaving the first mounted would make the second
        // assertion see both sets of links.
        const on = renderLinks({ [flag]: true } as Partial<FeatureFlags>);
        expect(renderedHrefs()).toContain(href);
        on.unmount();

        vi.clearAllMocks();
        renderLinks({ [flag]: false } as Partial<FeatureFlags>);
        expect(renderedHrefs()).not.toContain(href);
      },
    );

    it('advertises no gated page when every flag is off', () => {
      const allOff = Object.fromEntries(
        Object.keys(ALL_FEATURES_ENABLED).map((k) => [k, false]),
      ) as FeatureFlags;
      renderLinks(allOff);
      const hrefs = renderedHrefs();
      // Only the three ungated entries survive.
      expect(hrefs.sort()).toEqual(['/activity', '/runs', '/setup']);
    });
  });

  describe('administration group follows the current sidebar predicates', () => {
    it('is hidden from a plain member', () => {
      renderLinks({}, { isPlatformAdmin: false, isOrgAdmin: false });
      expect(screen.queryByText('Administration')).not.toBeInTheDocument();
      expect(renderedHrefs()).not.toContain('/budgets');
    });

    it('is shown to an org admin', () => {
      renderLinks({}, { isOrgAdmin: true });
      expect(screen.getByText('Administration')).toBeInTheDocument();
      expect(renderedHrefs()).toContain('/budgets');
      expect(renderedHrefs()).toContain('/ratelimits');
    });

    it('is shown to a platform admin', () => {
      renderLinks({}, { isPlatformAdmin: true });
      expect(renderedHrefs()).toContain('/budgets');
    });

    // The sidebar gates each admin entry on its own READ permission AND the role
    // (Navigation.tsx: `canViewBudgets() && (isPlatformAdmin() || isOrgAdmin())`).
    // An earlier revision of this component checked only the role, so an admin
    // without the permission was shown a link the server would refuse. These four
    // cases pin the permission dimension, which a role-only implementation passes
    // by accident.
    it('hides Budgets from an org admin who lacks BUDGET_READ', () => {
      renderLinks({}, { isOrgAdmin: true, canViewBudgets: false });
      expect(renderedHrefs()).not.toContain('/budgets');
    });

    it('hides Rate limits from an org admin who lacks RATELIMIT_READ', () => {
      renderLinks({}, { isOrgAdmin: true, canViewRateLimits: false });
      expect(renderedHrefs()).not.toContain('/ratelimits');
    });

    it('gates the two entries independently — one permission does not imply the other', () => {
      renderLinks({}, { isPlatformAdmin: true, canViewBudgets: true, canViewRateLimits: false });
      const hrefs = renderedHrefs();
      expect(hrefs).toContain('/budgets');
      expect(hrefs).not.toContain('/ratelimits');
    });

    it('drops the Administration heading entirely when an admin holds neither permission', () => {
      // An empty group heading would advertise a journey with nothing behind it.
      renderLinks({}, { isOrgAdmin: true, canViewBudgets: false, canViewRateLimits: false });
      expect(screen.queryByText('Administration')).not.toBeInTheDocument();
    });

    it('holding the permissions without an admin role is still not enough', () => {
      // The predicate is AND, not OR: a member with BUDGET_READ reaches the admin
      // CRUD page no more than the sidebar would let them.
      renderLinks({}, { canViewBudgets: true, canViewRateLimits: true });
      expect(screen.queryByText('Administration')).not.toBeInTheDocument();
      expect(renderedHrefs()).not.toContain('/budgets');
    });
  });

  describe('buildCurrentUiLinkGroups', () => {
    it('never emits a /next path — every link leaves the preview', () => {
      // The whole point of these entries is to reach the working current UI. A
      // /next target would be a stub, which the coexistence contract forbids.
      const groups = buildCurrentUiLinkGroups(features(), {
        isPlatformAdmin: true,
        isOrgAdmin: true,
        canViewBudgets: true,
        canViewRateLimits: true,
      });
      const all = groups.flatMap((g) => g.links);
      expect(all.length).toBeGreaterThan(0);
      for (const link of all) {
        expect(link.to.startsWith('/next')).toBe(false);
        expect(link.description).not.toBe('');
      }
    });

    it('emits unique destinations', () => {
      const all = buildCurrentUiLinkGroups(features(), {
        isPlatformAdmin: true,
        isOrgAdmin: true,
        canViewBudgets: true,
        canViewRateLimits: true,
      }).flatMap((g) => g.links.map((l) => l.to));
      expect(new Set(all).size).toBe(all.length);
    });
  });
});
