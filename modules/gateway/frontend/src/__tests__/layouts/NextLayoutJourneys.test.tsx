/**
 * Journey behaviour of NextLayout — Issue #5080.
 *
 * #5079's NextLayout.test.tsx is left intact as the shell regression signal (return
 * link, shared identity, shared WorkspaceSelector, preview labelling). This file
 * covers what NUI-02 adds, at the level where the acceptance criteria actually live:
 * which journeys an actor is offered, whether an unauthorized journey can be reached
 * by URL, whether a per-journey location is restored, and whether the navigation
 * works by keyboard and on a phone.
 *
 * The permission and feature hooks are stubbed per test so the role matrix can be
 * driven directly. `WorkspaceSelector` is stubbed for the reason #5079 gave: the
 * real one fetches on mount, and what matters here is that the layout renders the
 * same component the current UI does.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { NextLayout } from '@/layouts/NextLayout';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

const mockFeatures = vi.fn<() => FeatureFlags>();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockFeatures(),
}));

const mockPerms = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockPerms(),
}));

vi.mock('@/components/WorkspaceSelector', () => ({
  WorkspaceSelector: () => <div data-testid="workspace-selector">Organization</div>,
}));

interface Perms {
  isPlatformAdmin?: boolean;
  isOrgAdmin?: boolean;
  isDeptAdmin?: boolean;
  canViewOrganizations?: boolean;
  canViewBudgets?: boolean;
  canViewRateLimits?: boolean;
  canViewLogs?: boolean;
  canViewPool?: boolean;
  canViewMetrics?: boolean;
}

/** A plain member holds no roles and no permissions. */
const MEMBER: Perms = {};
const ORG_ADMIN: Perms = {
  isOrgAdmin: true,
  canViewOrganizations: true,
  canViewBudgets: true,
  canViewRateLimits: true,
};
const PLATFORM_ADMIN: Perms = {
  isPlatformAdmin: true,
  canViewOrganizations: true,
  canViewBudgets: true,
  canViewRateLimits: true,
  canViewLogs: true,
  canViewPool: true,
  canViewMetrics: true,
};

function setPerms(perms: Perms) {
  mockPerms.mockReturnValue({
    isPlatformAdmin: () => perms.isPlatformAdmin ?? false,
    isOrgAdmin: () => perms.isOrgAdmin ?? false,
    isDeptAdmin: () => perms.isDeptAdmin ?? false,
    canViewOrganizations: () => perms.canViewOrganizations ?? false,
    canViewBudgets: () => perms.canViewBudgets ?? false,
    canViewRateLimits: () => perms.canViewRateLimits ?? false,
    canViewLogs: () => perms.canViewLogs ?? false,
    canViewPool: () => perms.canViewPool ?? false,
    canViewMetrics: () => perms.canViewMetrics ?? false,
  });
}

function renderLayout({
  path = '/next',
  perms = MEMBER,
  flags = {},
  user = { id: 'u-1', githubLogin: 'octocat', orgId: 'org-1' },
}: {
  path?: string;
  perms?: Perms;
  flags?: Partial<FeatureFlags>;
  user?: Record<string, unknown> | null;
} = {}) {
  mockUseAuth.mockReturnValue({ user });
  mockFeatures.mockReturnValue({ ...ALL_FEATURES_ENABLED, ...flags });
  setPerms(perms);
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={<div data-testid="current-home">Current home</div>} />
        <Route path="/next" element={<NextLayout />}>
          <Route index element={<div data-testid="use-home">Use ADP home</div>} />
          <Route path="admin" element={<div data-testid="admin-home">Administration home</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('NextLayout journeys — Issue #5080', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
  });

  describe('members see Use ADP; administrators can enter Administration', () => {
    it('gives a member the Use ADP journey and no journey switch', () => {
      // Not a disabled Administration tab: withheld entirely.
      renderLayout({ perms: MEMBER });
      expect(screen.getByTestId('use-home')).toBeInTheDocument();
      expect(screen.queryByTestId('next-journey-switch')).not.toBeInTheDocument();
      expect(screen.queryByText('Administration')).not.toBeInTheDocument();
    });

    it('offers an org admin both journeys', () => {
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.getByTestId('next-journey-admin')).toHaveAttribute('href', '/next/admin');
    });

    it('offers a platform admin both journeys', () => {
      renderLayout({ perms: PLATFORM_ADMIN });
      expect(screen.getByTestId('next-journey-admin')).toBeInTheDocument();
    });

    it('offers Administration to a permitted non-admin log viewer', () => {
      // The design note requires that regrouping the system pages must not remove a
      // permitted viewer's access. Entry is keyed off surviving entries, not a role.
      renderLayout({ perms: { isDeptAdmin: true, canViewLogs: true } });
      expect(screen.getByTestId('next-journey-admin')).toBeInTheDocument();
    });

    it('shows a member and an admin the identical Use ADP journey', () => {
      // "Administration is a scope, not a competing menu tree": an administrator
      // starts in the same Use ADP experience a member gets, and Administration is
      // additive rather than a different set of user pages.
      const asMember = renderLayout({ perms: MEMBER });
      const memberEntries = screen
        .getAllByTestId(/^next-nav-entry-/)
        .map((el) => el.getAttribute('data-testid'));
      expect(memberEntries).toContain('next-nav-entry-runs');
      asMember.unmount();

      vi.clearAllMocks();
      renderLayout({ perms: PLATFORM_ADMIN });
      const adminEntries = screen
        .getAllByTestId(/^next-nav-entry-/)
        .map((el) => el.getAttribute('data-testid'));
      expect(adminEntries).toEqual(memberEntries);
    });
  });

  describe('an unauthorized journey cannot be reached by URL', () => {
    it('redirects a member away from /next/admin', () => {
      // A UI label is not authority, and the reverse also holds: reaching the URL
      // must not put a member in a journey they have no entries for.
      renderLayout({ path: '/next/admin', perms: MEMBER });
      expect(screen.queryByTestId('admin-home')).not.toBeInTheDocument();
      expect(screen.getByTestId('use-home')).toBeInTheDocument();
    });

    it('lets an admin deep-link straight into /next/admin', () => {
      // Bookmarks and deep links into either journey must land in the right one.
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });
      expect(screen.getByTestId('admin-home')).toBeInTheDocument();
      expect(screen.getByTestId('next-journey-admin')).toHaveAttribute('aria-current', 'page');
    });

    it('redirects an actor whose only administration entry lost its feature flag', () => {
      // Effective availability decides, not the role — and the converse of the log
      // viewer case above. The same permitted viewer who IS offered Administration
      // when `logs` is on has nothing to administer when the flag is off, so the
      // journey closes rather than presenting an empty page.
      //
      // Note this actor is deliberately not a platform or org admin: those two always
      // retain Access requests and GitHub org links, which carry no feature gate, so
      // no combination of flags empties their journey.
      renderLayout({
        path: '/next/admin',
        perms: { isDeptAdmin: true, canViewLogs: true },
        flags: { logs: false },
      });
      expect(screen.queryByTestId('admin-home')).not.toBeInTheDocument();
      expect(screen.getByTestId('use-home')).toBeInTheDocument();
      expect(screen.queryByTestId('next-journey-switch')).not.toBeInTheDocument();
    });
  });

  describe('the return link and the journey switch coexist', () => {
    it('renders both controls at once', () => {
      // #5079's contract: the switch moves between journeys inside the preview and
      // must not replace the control that leaves it.
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.getByTestId('back-to-current-ui')).toHaveAttribute('href', '/');
      expect(screen.getByTestId('next-journey-switch')).toBeInTheDocument();
    });

    it('keeps the return link on the administration journey too', () => {
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });
      expect(screen.getByTestId('back-to-current-ui')).toBeInTheDocument();
    });

    it('keeps the shared WorkspaceSelector in both journeys', () => {
      // Organization context is preserved across the journeys, not duplicated.
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });
      expect(screen.getByTestId('workspace-selector')).toBeInTheDocument();
    });
  });

  describe('per-journey return location', () => {
    it('sends the switch to a journey’s remembered location', () => {
      // Record a Use ADP location, move to Administration, and the Use ADP tab
      // should point back at where we were rather than at the journey home.
      window.sessionStorage.setItem(
        'adp.next.journeyMemory.v1',
        JSON.stringify({ scope: 'u-1::org-1', journeys: { use: '/next' } }),
      );
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });
      expect(screen.getByTestId('next-journey-use')).toHaveAttribute('href', '/next');
    });

    it('records the current location as the active journey’s', () => {
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });
      const raw = window.sessionStorage.getItem('adp.next.journeyMemory.v1') ?? '';
      expect(raw).toContain('/next/admin');
    });

    it('falls back to the journey home when a remembered location is not restorable', () => {
      // A path recorded by a build that shipped more preview pages than this one, or
      // one the actor's permissions no longer reach. Following it would land the user
      // on a route this story does not ship, so the switch must degrade to the
      // journey home rather than pass the stale path through.
      window.sessionStorage.setItem(
        'adp.next.journeyMemory.v1',
        JSON.stringify({
          scope: 'u-1::org-1',
          journeys: { admin: '/next/admin/budgets/deleted-org' },
        }),
      );
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.getByTestId('next-journey-admin')).toHaveAttribute('href', '/next/admin');
    });

    it('withholds Administration entirely when the actor can no longer administer', () => {
      // Stronger than falling back to the home page: if the role behind a remembered
      // administration location is gone, the journey is not offered at all, so the
      // stored path is unreachable rather than merely rewritten.
      window.sessionStorage.setItem(
        'adp.next.journeyMemory.v1',
        JSON.stringify({ scope: 'u-1::org-1', journeys: { admin: '/next/admin' } }),
      );
      renderLayout({ perms: MEMBER });
      expect(screen.queryByTestId('next-journey-admin')).not.toBeInTheDocument();
    });

    it('discards a previous organization’s recorded location on first render', () => {
      // Tenant isolation at the layout level. Whether such a value is *readable* is
      // asserted in useJourneyMemory.test.tsx — it cannot be proved here, because
      // this story ships only the two journey homes, so a rejected memory and a
      // fallback resolve to the same href and the assertion would hold either way.
      //
      // What is observable here is that simply mounting the layout in org-1 removes
      // org-OTHER's record, so the prior tenant's position does not sit in storage
      // waiting for a future preview route to restore it.
      window.sessionStorage.setItem(
        'adp.next.journeyMemory.v1',
        JSON.stringify({ scope: 'u-1::org-OTHER', journeys: { use: '/next/other-tenant' } }),
      );
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });

      const raw = window.sessionStorage.getItem('adp.next.journeyMemory.v1') ?? '';
      expect(raw).not.toContain('/next/other-tenant');
      expect(raw).not.toContain('org-OTHER');
      expect(screen.getByTestId('next-journey-use')).toHaveAttribute('href', '/next');
    });
  });

  describe('active page state', () => {
    it('marks the active journey in the switch', () => {
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.getByTestId('next-journey-use')).toHaveAttribute('aria-current', 'page');
      expect(screen.getByTestId('next-journey-admin')).not.toHaveAttribute('aria-current');
    });

    it('names the current journey in the main region', () => {
      renderLayout({ path: '/next/admin', perms: ORG_ADMIN });
      const main = screen.getByRole('main');
      expect(main).toHaveTextContent('Administration');
    });
  });

  describe('keyboard and mobile navigation', () => {
    it('keeps the skip link to the main content', () => {
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.getByText('Skip to main content')).toHaveAttribute(
        'href',
        '#next-main-content',
      );
    });

    it('opens and closes the mobile navigation drawer', () => {
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.queryByTestId('next-nav-drawer')).not.toBeInTheDocument();

      fireEvent.click(screen.getByTestId('next-nav-toggle'));
      expect(screen.getByTestId('next-nav-drawer')).toBeInTheDocument();
      expect(screen.getByTestId('next-nav-toggle')).toHaveAttribute('aria-expanded', 'true');

      fireEvent.click(screen.getByTestId('next-nav-drawer-close'));
      expect(screen.queryByTestId('next-nav-drawer')).not.toBeInTheDocument();
    });

    it('closes the drawer on Escape', () => {
      renderLayout({ perms: ORG_ADMIN });
      fireEvent.click(screen.getByTestId('next-nav-toggle'));
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(screen.queryByTestId('next-nav-drawer')).not.toBeInTheDocument();
    });

    it('describes the drawer trigger for assistive technology', () => {
      renderLayout({ perms: ORG_ADMIN });
      const toggle = screen.getByTestId('next-nav-toggle');
      expect(toggle).toHaveAttribute('aria-controls', 'next-nav-drawer');
      expect(toggle).toHaveAccessibleName('Open navigation');
    });

    it('does not render the drawer’s links while it is closed', () => {
      // Mounted only when open, so hidden links are not focusable — otherwise Tab
      // would traverse an invisible copy of the whole navigation.
      renderLayout({ perms: ORG_ADMIN });
      expect(screen.getAllByTestId('next-nav')).toHaveLength(1);
      fireEvent.click(screen.getByTestId('next-nav-toggle'));
      expect(screen.getAllByTestId('next-nav')).toHaveLength(2);
    });
  });

  describe('resilience', () => {
    it('renders without throwing when auth state is momentarily empty', () => {
      // The guards above normally prevent it, but the navigation must not crash the
      // preview if the session is briefly absent.
      renderLayout({ user: null, perms: MEMBER });
      expect(screen.getByTestId('next-layout')).toBeInTheDocument();
      expect(screen.queryByTestId('workspace-selector')).not.toBeInTheDocument();
    });
  });
});
