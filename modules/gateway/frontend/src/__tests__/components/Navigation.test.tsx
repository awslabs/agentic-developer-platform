/**
 * Tests for Navigation component — Issue #3590.
 *
 * Verifies: GitLab link renders for authenticated users, uses a plain <a> tag
 * (not NavLink), and has href="/gitlab".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Navigation } from '@/components/Navigation';

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

// Mock useFeatures — all features enabled (fail-open default)
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => ({
    chat: true,
    knowledge: true,
    indexing: true,
    connections: true,
    credentials: true,
  }),
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
  });

  describe('GitLab link', () => {
    it('renders a GitLab link visible to all authenticated users', () => {
      renderNavigation();

      const gitlabLink = screen.getByText('GitLab');
      expect(gitlabLink).toBeInTheDocument();
    });

    it('uses a plain <a> tag, not a NavLink (full page navigation)', () => {
      renderNavigation();

      const gitlabLink = screen.getByText('GitLab').closest('a');
      expect(gitlabLink).not.toBeNull();
      expect(gitlabLink!.tagName).toBe('A');
      // NavLink renders with data-discover attribute; plain <a> does not
      expect(gitlabLink!.getAttribute('data-discover')).toBeNull();
    });

    it('has href="/gitlab"', () => {
      renderNavigation();

      const gitlabLink = screen.getByText('GitLab').closest('a');
      expect(gitlabLink).toHaveAttribute('href', '/gitlab');
    });

    it('displays the fox emoji icon', () => {
      renderNavigation();

      const gitlabLink = screen.getByText('GitLab').closest('a');
      expect(gitlabLink).not.toBeNull();
      expect(gitlabLink!.textContent).toContain('🦊');
    });
  });
});
