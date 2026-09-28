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

// Mock usePermissions — return a basic authenticated user (no admin roles)
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
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
  }),
}));

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
});
