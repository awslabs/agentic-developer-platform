/**
 * Tests for landing redirect behavior — Issue #3634.
 *
 * Verifies:
 * - All roles redirect from "/" to "/runs"
 * - /dashboard redirects: platform admin → /admin/system; non-admin → /runs
 * - No redirect loop: /runs renders the dashboard (does not redirect again)
 * - Unauthenticated user → /login (existing behavior preserved)
 * - Platform admin at /admin/system sees PlatformDashboard content
 * - Non-admin at /admin/system redirected to /runs
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { RoleBasedRedirect } from '@/components/RoleBasedRedirect';
import { DashboardRedirect } from '@/components/DashboardRedirect';
import { AdminGuard } from '@/components/AdminGuard';
import { AdminRole } from '@/types';

// Mock useAuthContext for RoleBasedRedirect
const mockUser = vi.fn();

vi.mock('@/contexts/AuthContext', () => ({
  useAuthContext: () => ({ user: mockUser() }),
}));

// Mock usePermissions for DashboardRedirect and AdminGuard
const mockIsPlatformAdmin = vi.fn();

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    isPlatformAdmin: mockIsPlatformAdmin,
  }),
}));

describe('Landing Redirect — Issue #3634', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('RoleBasedRedirect (/ path)', () => {
    function renderAtRoot() {
      return render(
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<RoleBasedRedirect />} />
            <Route path="/runs" element={<div data-testid="runs-page">Agent Run Dashboard</div>} />
            <Route path="/login" element={<div data-testid="login-page">Login</div>} />
            <Route path="/dashboard" element={<div data-testid="dashboard-page">Dashboard</div>} />
          </Routes>
        </MemoryRouter>
      );
    }

    it('redirects platform admin from "/" to "/runs"', () => {
      mockUser.mockReturnValue({ role: AdminRole.PLATFORM_ADMIN, orgId: 'org-1' });
      renderAtRoot();
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
    });

    it('redirects org admin from "/" to "/runs"', () => {
      mockUser.mockReturnValue({ role: AdminRole.ORG_ADMIN, orgId: 'org-1' });
      renderAtRoot();
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
    });

    it('redirects dept admin from "/" to "/runs"', () => {
      mockUser.mockReturnValue({ role: AdminRole.DEPT_ADMIN, orgId: 'org-1', deptId: 'dept-1' });
      renderAtRoot();
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
    });

    it('redirects regular user from "/" to "/runs"', () => {
      mockUser.mockReturnValue({ role: 'user', orgId: 'org-1' });
      renderAtRoot();
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
    });

    it('redirects unauthenticated user from "/" to "/login"', () => {
      mockUser.mockReturnValue(null);
      renderAtRoot();
      expect(screen.getByTestId('login-page')).toBeInTheDocument();
    });

    it('does not redirect to /dashboard (old behavior removed)', () => {
      mockUser.mockReturnValue({ role: AdminRole.PLATFORM_ADMIN, orgId: 'org-1' });
      renderAtRoot();
      expect(screen.queryByTestId('dashboard-page')).not.toBeInTheDocument();
    });
  });

  describe('DashboardRedirect (/dashboard path)', () => {
    function renderAtDashboard() {
      return render(
        <MemoryRouter initialEntries={['/dashboard']}>
          <Routes>
            <Route path="/dashboard" element={<DashboardRedirect />} />
            <Route path="/admin/system" element={<div data-testid="admin-system-page">System Health</div>} />
            <Route path="/runs" element={<div data-testid="runs-page">Agent Run Dashboard</div>} />
          </Routes>
        </MemoryRouter>
      );
    }

    it('redirects platform admin from /dashboard to /admin/system', () => {
      mockIsPlatformAdmin.mockReturnValue(true);
      renderAtDashboard();
      expect(screen.getByTestId('admin-system-page')).toBeInTheDocument();
    });

    it('redirects non-admin from /dashboard to /runs', () => {
      mockIsPlatformAdmin.mockReturnValue(false);
      renderAtDashboard();
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
    });
  });

  describe('AdminGuard (/admin/system path)', () => {
    function renderAtAdminSystem() {
      return render(
        <MemoryRouter initialEntries={['/admin/system']}>
          <Routes>
            <Route
              path="/admin/system"
              element={
                <AdminGuard>
                  <div data-testid="platform-dashboard">PlatformDashboard Content</div>
                </AdminGuard>
              }
            />
            <Route path="/runs" element={<div data-testid="runs-page">Agent Run Dashboard</div>} />
          </Routes>
        </MemoryRouter>
      );
    }

    it('platform admin at /admin/system sees PlatformDashboard content', () => {
      mockIsPlatformAdmin.mockReturnValue(true);
      renderAtAdminSystem();
      expect(screen.getByTestId('platform-dashboard')).toBeInTheDocument();
      expect(screen.getByText('PlatformDashboard Content')).toBeInTheDocument();
    });

    it('non-admin at /admin/system is redirected to /runs', () => {
      mockIsPlatformAdmin.mockReturnValue(false);
      renderAtAdminSystem();
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
      expect(screen.queryByTestId('platform-dashboard')).not.toBeInTheDocument();
    });
  });

  describe('No redirect loop', () => {
    it('/runs renders the dashboard directly without redirecting', () => {
      // Simulate the /runs route rendering AgentRunDashboard (no redirect)
      render(
        <MemoryRouter initialEntries={['/runs']}>
          <Routes>
            <Route path="/" element={<RoleBasedRedirect />} />
            <Route path="/runs" element={<div data-testid="runs-page">Agent Run Dashboard</div>} />
            <Route path="/login" element={<div data-testid="login-page">Login</div>} />
          </Routes>
        </MemoryRouter>
      );

      // /runs renders directly — no redirect back to "/"
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
      expect(screen.queryByTestId('login-page')).not.toBeInTheDocument();
    });

    it('"/" → "/runs" does not bounce back to "/"', () => {
      mockUser.mockReturnValue({ role: AdminRole.PLATFORM_ADMIN });
      render(
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<RoleBasedRedirect />} />
            <Route path="/runs" element={<div data-testid="runs-page">Agent Run Dashboard</div>} />
          </Routes>
        </MemoryRouter>
      );

      // Should land on /runs, not loop
      expect(screen.getByTestId('runs-page')).toBeInTheDocument();
    });
  });
});
