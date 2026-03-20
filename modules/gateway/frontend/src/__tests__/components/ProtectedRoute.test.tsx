import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { ProtectedRoute } from '@/components/ProtectedRoute';
import { AdminRole, Permission } from '@/types';
import type { ReactNode } from 'react';

// Mock the useAuth hook
const mockUseAuth = vi.fn();

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

// Mock LoadingScreen component
vi.mock('@/components/LoadingScreen', () => ({
  LoadingScreen: () => <div data-testid="loading-screen">Loading...</div>,
}));

interface RenderOptions {
  initialPath?: string;
}

function renderWithRouter(
  ui: ReactNode,
  { initialPath = '/protected' }: RenderOptions = {}
) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/login" element={<div data-testid="login-page">Login Page</div>} />
        <Route path="/unauthorized" element={<div data-testid="unauthorized-page">Unauthorized</div>} />
        <Route path="/protected" element={ui} />
        <Route path="/dashboard" element={ui} />
      </Routes>
    </MemoryRouter>
  );
}

describe('ProtectedRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Loading state', () => {
    it('renders loading screen when auth is loading', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: false,
        isLoading: true,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute>
          <div data-testid="protected-content">Protected Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('loading-screen')).toBeInTheDocument();
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });
  });

  describe('Authentication checks', () => {
    it('renders children when user is authenticated', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: () => true,
      });

      renderWithRouter(
        <ProtectedRoute>
          <div data-testid="protected-content">Protected Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
      expect(screen.getByText('Protected Content')).toBeInTheDocument();
    });

    it('redirects to login when not authenticated', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: false,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute>
          <div data-testid="protected-content">Protected Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('login-page')).toBeInTheDocument();
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });

    it('does not render protected content when unauthenticated', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: false,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute>
          <div data-testid="protected-content">Secret Data</div>
        </ProtectedRoute>
      );

      expect(screen.queryByText('Secret Data')).not.toBeInTheDocument();
    });
  });

  describe('Permission-based access control', () => {
    it('renders children when user has required permission', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: (perm: Permission) => perm === Permission.ORG_READ,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requiredPermission={Permission.ORG_READ}>
          <div data-testid="protected-content">Organization Data</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('redirects to unauthorized when user lacks required permission', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requiredPermission={Permission.POOL_MANAGE}>
          <div data-testid="protected-content">Pool Management</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });

    it('checks specific permission correctly', () => {
      const hasPermissionMock = vi.fn((perm: Permission) => perm === Permission.BUDGET_READ);

      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: hasPermissionMock,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requiredPermission={Permission.BUDGET_READ}>
          <div data-testid="protected-content">Budget Info</div>
        </ProtectedRoute>
      );

      expect(hasPermissionMock).toHaveBeenCalledWith(Permission.BUDGET_READ);
      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });
  });

  describe('Role-based access control', () => {
    it('renders children when user has required role', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      renderWithRouter(
        <ProtectedRoute requiredRole={AdminRole.PLATFORM_ADMIN}>
          <div data-testid="protected-content">Admin Panel</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('redirects to unauthorized when user lacks required role', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: (role: AdminRole) => role === AdminRole.DEPT_ADMIN,
      });

      renderWithRouter(
        <ProtectedRoute requiredRole={AdminRole.PLATFORM_ADMIN}>
          <div data-testid="protected-content">Platform Admin Only</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });

    it('org admin cannot access platform admin routes', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: (role: AdminRole) => role === AdminRole.ORG_ADMIN,
      });

      renderWithRouter(
        <ProtectedRoute requiredRole={AdminRole.PLATFORM_ADMIN}>
          <div data-testid="protected-content">Platform Settings</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
    });
  });

  describe('requireAny permissions', () => {
    it('renders children when user has any of the required permissions', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: (perm: Permission) => perm === Permission.LOGS_READ,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requireAny={[Permission.LOGS_READ, Permission.LOGS_EXPORT]}>
          <div data-testid="protected-content">Logs</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('redirects to unauthorized when user has none of the required permissions', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requireAny={[Permission.POOL_READ, Permission.POOL_MANAGE]}>
          <div data-testid="protected-content">Pool</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
    });

    it('handles empty requireAny array', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requireAny={[]}>
          <div data-testid="protected-content">Content</div>
        </ProtectedRoute>
      );

      // Empty array should not trigger permission check
      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('checks all permissions in requireAny', () => {
      const hasPermissionMock = vi.fn().mockReturnValue(false);

      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: hasPermissionMock,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requireAny={[Permission.ORG_CREATE, Permission.ORG_DELETE]}>
          <div data-testid="protected-content">Content</div>
        </ProtectedRoute>
      );

      expect(hasPermissionMock).toHaveBeenCalledWith(Permission.ORG_CREATE);
      expect(hasPermissionMock).toHaveBeenCalledWith(Permission.ORG_DELETE);
    });
  });

  describe('Combined requirements', () => {
    it('requires both authentication and permission', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requiredPermission={Permission.USER_MANAGE}>
          <div data-testid="protected-content">User Management</div>
        </ProtectedRoute>
      );

      // Authenticated but missing permission
      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
    });

    it('requires both authentication and role', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute requiredRole={AdminRole.PLATFORM_ADMIN}>
          <div data-testid="protected-content">Admin Area</div>
        </ProtectedRoute>
      );

      // Authenticated but wrong role
      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
    });

    it('works with both requiredPermission and requiredRole', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: (perm: Permission) => perm === Permission.POOL_MANAGE,
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      renderWithRouter(
        <ProtectedRoute
          requiredPermission={Permission.POOL_MANAGE}
          requiredRole={AdminRole.PLATFORM_ADMIN}
        >
          <div data-testid="protected-content">Pool Management</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('fails when permission passes but role fails', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute
          requiredPermission={Permission.BUDGET_READ}
          requiredRole={AdminRole.PLATFORM_ADMIN}
        >
          <div data-testid="protected-content">Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('unauthorized-page')).toBeInTheDocument();
    });
  });

  describe('Children rendering', () => {
    it('renders multiple children correctly', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: () => true,
      });

      renderWithRouter(
        <ProtectedRoute>
          <div data-testid="child1">Child 1</div>
          <div data-testid="child2">Child 2</div>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('child1')).toBeInTheDocument();
      expect(screen.getByTestId('child2')).toBeInTheDocument();
    });

    it('renders complex nested components', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => true,
        hasRole: () => true,
      });

      renderWithRouter(
        <ProtectedRoute>
          <main>
            <header data-testid="header">Header</header>
            <section data-testid="content">Main Content</section>
            <footer data-testid="footer">Footer</footer>
          </main>
        </ProtectedRoute>
      );

      expect(screen.getByTestId('header')).toBeInTheDocument();
      expect(screen.getByTestId('content')).toBeInTheDocument();
      expect(screen.getByTestId('footer')).toBeInTheDocument();
    });
  });

  describe('No requirements specified', () => {
    it('only checks authentication when no permissions or roles specified', () => {
      mockUseAuth.mockReturnValue({
        isAuthenticated: true,
        isLoading: false,
        hasPermission: () => false,
        hasRole: () => false,
      });

      renderWithRouter(
        <ProtectedRoute>
          <div data-testid="protected-content">Basic Protected Content</div>
        </ProtectedRoute>
      );

      // Should render because user is authenticated, even without specific permissions
      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });
  });
});
