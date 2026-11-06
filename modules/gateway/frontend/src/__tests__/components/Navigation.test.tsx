/** Navigation visibility and permission gates. */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Navigation } from '@/components/Navigation';
import { Permission } from '@/types';

// Mock usePermissions — defaults to a basic authenticated user (no admin roles).
// Individual tests override the role predicates via mockPermissions.
const mockUsePermissions = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockUsePermissions(),
}));

function permissions(overrides: Record<string, unknown> = {}) {
  return {
    isPlatformAdmin: () => false,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    user: { orgId: 'org-1', deptId: 'dept-1' },
    canViewOrganizations: () => false,
    canViewLogs: () => false,
    canViewMetrics: () => false,
    canViewPool: () => false,
    canViewBudgets: () => false,
    canViewRateLimits: () => false,
    hasPermission: () => false,
    ...overrides,
  };
}

// Mock useFeatures — default: all features enabled, gitlab disabled (fail-closed)
const mockUseFeatures = vi.fn();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
}));

function renderNavigation() {
  return render(
    <MemoryRouter>
      <Navigation />
    </MemoryRouter>
  );
}

describe('Navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(permissions());
    // Default: all core features enabled, gitlab disabled (fail-closed)
    mockUseFeatures.mockReturnValue({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
      system_dashboard: true,
      logs: true,
      gitlab: false,
      agent_models: false,
    });
  });

  it('shows tool access only to a principal with the dedicated permission', () => {
    renderNavigation();
    expect(screen.queryByText('Tool access')).not.toBeInTheDocument();
    mockUsePermissions.mockReturnValue(permissions({ hasPermission: (permission: Permission) => permission === Permission.TOOLS_MANAGE }));
    renderNavigation();
    expect(screen.getByText('Tool access').closest('a')).toHaveAttribute('href', '/admin/tools');
  });

  describe('Agent Models link (feature-gated, Issue #5422)', () => {
    it('is absent until the strict rollout flag is enabled', () => {
      renderNavigation();
      expect(screen.queryByText('Agent Models')).not.toBeInTheDocument();
    });

    it('is available to an ordinary authenticated user when enabled', () => {
      mockUseFeatures.mockReturnValue({
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: false,
        agent_models: true,
      });
      renderNavigation();
      expect(screen.getByText('Agent Models').closest('a')).toHaveAttribute(
        'href',
        '/settings/agent-models',
      );
    });
  });

  describe('Release navigation', () => {
    it.each([false, true])('hides deferred destinations even with their flags set to %s', (enabled) => {
      mockUseFeatures.mockReturnValue({ chat: enabled, gitlab: enabled, indexing: enabled, superplane: enabled });
      mockUsePermissions.mockReturnValue(permissions({ isPlatformAdmin: () => true }));
      renderNavigation();
      for (const label of ['Agent Chat', 'My conversations', 'GitLab', 'Indexing Status', 'Superplane']) {
        expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument();
      }
      expect(screen.getByRole('link', { name: 'Dashboard' })).toBeInTheDocument();
    });
  });

  // Issue #4018: org admins review the join-my-org requests for their own
  // tenant, so the link is no longer platform-admin-only. This is a COSMETIC
  // gate (it reads the `custom:role` claim); the server enforces the real
  // scope, so these tests pin visibility only, never authority.
  describe('Access Requests link (org-scoped, Issue #4018)', () => {
    it('renders for a platform admin', () => {
      mockUsePermissions.mockReturnValue(permissions({ isPlatformAdmin: () => true }));

      renderNavigation();

      expect(screen.getByText('Access Requests')).toBeInTheDocument();
    });

    it('renders for an org admin', () => {
      mockUsePermissions.mockReturnValue(permissions({ isOrgAdmin: () => true }));

      renderNavigation();

      expect(screen.getByText('Access Requests')).toBeInTheDocument();
    });

    it('does NOT render for a dept admin', () => {
      mockUsePermissions.mockReturnValue(permissions({ isDeptAdmin: () => true }));

      renderNavigation();

      expect(screen.queryByText('Access Requests')).not.toBeInTheDocument();
    });

    it('does NOT render for a plain member', () => {
      renderNavigation();

      expect(screen.queryByText('Access Requests')).not.toBeInTheDocument();
    });

    it('points at /admin/access-requests', () => {
      mockUsePermissions.mockReturnValue(permissions({ isOrgAdmin: () => true }));

      renderNavigation();

      expect(screen.getByText('Access Requests').closest('a')).toHaveAttribute(
        'href',
        '/admin/access-requests'
      );
    });
  });

  // Issue #4841. Two entries would otherwise both read "Organizations": the
  // system-dashboard usage anchor and the new structure panel. These pin the
  // disambiguation so a future edit cannot silently restore the collision.
  describe('Organizations links (Issue #4841)', () => {
    it('renders the structure panel for anyone with ORG_READ, including an org admin', () => {
      // Not platform-admin-gated: the list route filters to the caller's own org, and the
      // dept/team writes gate on ORG_UPDATE scoped to target_org_id.
      mockUsePermissions.mockReturnValue(
        permissions({ isOrgAdmin: () => true, canViewOrganizations: () => true })
      );

      renderNavigation();

      expect(screen.getByText('Organizations').closest('a')).toHaveAttribute(
        'href',
        '/admin/organizations'
      );
    });

    it('does NOT render the structure panel without ORG_READ', () => {
      renderNavigation();

      expect(screen.queryByText('Organizations')).not.toBeInTheDocument();
    });

    it('labels the system-dashboard usage anchor "Org Usage", not "Organizations"', () => {
      // A platform admin sees BOTH entries. Before the relabel they were both called
      // "Organizations", with no way to tell which one managed structure.
      mockUsePermissions.mockReturnValue(
        permissions({ isPlatformAdmin: () => true, canViewOrganizations: () => true })
      );

      renderNavigation();

      expect(screen.getByText('Org Usage').closest('a')).toHaveAttribute(
        'href',
        '/admin/system#organizations'
      );
      // Exactly one "Organizations" entry, and it is the structure panel.
      expect(screen.getAllByText('Organizations')).toHaveLength(1);
      expect(screen.getByText('Organizations').closest('a')).toHaveAttribute(
        'href',
        '/admin/organizations'
      );
    });
  });

  describe('Apps & CLI Setup link', () => {
    it('names both apps and CLI tools', () => {
      renderNavigation();

      expect(screen.getByText('Apps & CLI Setup')).toBeInTheDocument();
      expect(screen.queryByText('Claude Code Setup')).not.toBeInTheDocument();
    });

    it('still points at /setup (rename must not break the link)', () => {
      renderNavigation();

      expect(screen.getByText('Apps & CLI Setup').closest('a')).toHaveAttribute('href', '/setup');
    });
  });
});


describe('optional menu entries', () => {
  it.each([true, false])('honors menu flags for a platform admin: %s', (enabled) => {
    mockUsePermissions.mockReturnValue(permissions({ isPlatformAdmin: () => true }));
    mockUseFeatures.mockReturnValue({ tenant_org_links: enabled, knowledge: enabled, indexing: enabled });
    renderNavigation();
    for (const label of ['Tenant Org Links', 'Context']) {
      if (enabled) expect(screen.getByText(label)).toBeInTheDocument();
      else expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });
});


describe('Dev Box is the single entry (#8864)', () => {
  it.each([
    { chat: true, origin: 'https://crew.example.test', assistant_ui: true, crew_dashboard: true },
    { chat: false, origin: undefined, assistant_ui: false, crew_dashboard: true },
  ])('never renders the retired Crew Dashboard link or Runtime item: %j', ({ chat, origin, assistant_ui, crew_dashboard }) => {
    mockUseFeatures.mockReturnValue({ chat, crew_dashboard_origin: origin, assistant_ui, crew_dashboard });
    renderNavigation();
    expect(screen.queryByText('Crew Dashboard')).not.toBeInTheDocument();
    expect(screen.queryByText('Runtime')).not.toBeInTheDocument();
    expect(screen.getByText('Dev Box').closest('a')).toHaveAttribute('href', '/settings/dev-box');
  });
});
