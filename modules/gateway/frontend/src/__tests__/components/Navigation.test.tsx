/**
 * Tests for Navigation component — Issues #3590, #3773.
 *
 * Verifies: GitLab link is feature-gated behind FEATURE_GITLAB_ENABLED
 * (fail-closed). When enabled, renders as a plain <a> tag (not NavLink),
 * with href="/gitlab/" (trailing slash — the CloudFront /gitlab/* behavior
 * does not match the bare /gitlab path).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Navigation } from '@/components/Navigation';

// Mock auth service — getAccessToken returns null by default (no SSO redirect)
vi.mock('@/services/auth', () => ({
  getAccessToken: () => null,
}));

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
    });
  });

  describe('GitLab link (feature-gated, Issue #3773)', () => {
    it('does NOT render when features.gitlab is false (fail-closed default)', () => {
      renderNavigation();

      expect(screen.queryByText('GitLab')).not.toBeInTheDocument();
    });

    it('renders when features.gitlab is true', () => {
      mockUseFeatures.mockReturnValue({
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: true,
      });

      renderNavigation();

      const gitlabLink = screen.getByText('GitLab');
      expect(gitlabLink).toBeInTheDocument();
    });

    it('uses a plain <a> tag, not a NavLink (full page navigation)', () => {
      mockUseFeatures.mockReturnValue({
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: true,
      });

      renderNavigation();

      const gitlabLink = screen.getByText('GitLab').closest('a');
      expect(gitlabLink).not.toBeNull();
      expect(gitlabLink!.tagName).toBe('A');
      // NavLink renders with data-discover attribute; plain <a> does not
      expect(gitlabLink!.getAttribute('data-discover')).toBeNull();
    });

    it('has href="/gitlab/" (trailing slash required by CloudFront /gitlab/* behavior)', () => {
      mockUseFeatures.mockReturnValue({
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: true,
      });

      renderNavigation();

      const gitlabLink = screen.getByText('GitLab').closest('a');
      expect(gitlabLink).toHaveAttribute('href', '/gitlab/');
    });

    it('displays the fox emoji icon when enabled', () => {
      mockUseFeatures.mockReturnValue({
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: true,
      });

      renderNavigation();

      const gitlabLink = screen.getByText('GitLab').closest('a');
      expect(gitlabLink).not.toBeNull();
      expect(gitlabLink!.textContent).toContain('🦊');
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

  describe('CLI Setup link (Issue #4159)', () => {
    it('is labelled "CLI Setup", not "Claude Code Setup"', () => {
      // The page covers Codex too; the old label hid that from Codex users.
      renderNavigation();

      expect(screen.getByText('CLI Setup')).toBeInTheDocument();
      expect(screen.queryByText('Claude Code Setup')).not.toBeInTheDocument();
    });

    it('still points at /setup (rename must not break the link)', () => {
      renderNavigation();

      expect(screen.getByText('CLI Setup').closest('a')).toHaveAttribute('href', '/setup');
    });
  });
});
