import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { renderHook } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider, useAuthContext } from '@/contexts/AuthContext';
import { AdminRole, Permission } from '@/types';
import type { ReactNode } from 'react';

// Mock navigate
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

// Mock auth service functions
vi.mock('@/services/auth', () => ({
  buildLoginUrl: vi.fn(),
  refreshToken: vi.fn(),
  logout: vi.fn(),
  logoutWithRedirect: vi.fn(),
  getCurrentUserFromToken: vi.fn(),
  getAccessToken: vi.fn(),
  getTokenExpiry: vi.fn(),
  isTokenExpired: vi.fn(),
  clearTokens: vi.fn(),
}));

import * as authService from '@/services/auth';

function createWrapper() {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <BrowserRouter>
        <AuthProvider>{children}</AuthProvider>
      </BrowserRouter>
    );
  };
}

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe('AuthProvider', () => {
    it('renders children correctly', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue(null);

      render(
        <BrowserRouter>
          <AuthProvider>
            <div data-testid="child">Child Content</div>
          </AuthProvider>
        </BrowserRouter>
      );

      expect(screen.getByTestId('child')).toBeInTheDocument();
      expect(screen.getByText('Child Content')).toBeInTheDocument();
    });

    it('initializes as not authenticated when no token', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue(null);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.isAuthenticated).toBe(false);
      expect(result.current.user).toBeNull();
    });

    it('restores session from stored token', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.PLATFORM_ADMIN,
        permissions: [Permission.ORG_READ, Permission.POOL_MANAGE],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('valid-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(false);
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.isAuthenticated).toBe(true);
      expect(result.current.user).toEqual(mockUser);
    });

    it('refreshes expired token on initialization', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.ORG_ADMIN,
        orgId: 'org-1',
        permissions: [Permission.ORG_READ],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('expired-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(true);
      vi.mocked(authService.refreshToken).mockResolvedValue({
        token: 'new-token',
        expiresAt: new Date(Date.now() + 3600000).toISOString(),
      });
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(authService.refreshToken).toHaveBeenCalled();
      expect(result.current.user).toEqual(mockUser);
    });

    it('clears tokens when token refresh fails', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue('expired-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(true);
      vi.mocked(authService.refreshToken).mockRejectedValue(new Error('Refresh failed'));
      vi.mocked(authService.clearTokens).mockImplementation(() => {});

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(authService.clearTokens).toHaveBeenCalled();
      expect(result.current.isAuthenticated).toBe(false);
    });
  });

  describe('login', () => {
    it('redirects to Cognito hosted UI', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue(null);
      vi.mocked(authService.buildLoginUrl).mockResolvedValue('https://cognito.example.com/login');

      // Mock window.location.href setter
      const locationHrefSpy = vi.spyOn(window, 'location', 'get');
      const mockLocation = { href: '' } as Location;
      locationHrefSpy.mockReturnValue(mockLocation);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      await act(async () => {
        // Note: login() will try to redirect, which we can't fully test in Jest
        // But we can verify buildLoginUrl was called
        try {
          await result.current.login();
        } catch {
          // Expected - can't actually redirect in test
        }
      });

      expect(authService.buildLoginUrl).toHaveBeenCalled();
      locationHrefSpy.mockRestore();
    });
  });

  describe('logout', () => {
    it('clears auth state and redirects to login', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.PLATFORM_ADMIN,
        permissions: [Permission.ORG_READ],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('valid-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(false);
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);
      vi.mocked(authService.logout).mockResolvedValue();
      vi.mocked(authService.clearTokens).mockImplementation(() => {});

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isAuthenticated).toBe(true);
      });

      await act(async () => {
        await result.current.logout();
      });

      expect(authService.logout).toHaveBeenCalled();
      expect(authService.clearTokens).toHaveBeenCalled();
      expect(result.current.isAuthenticated).toBe(false);
      expect(result.current.user).toBeNull();
      expect(mockNavigate).toHaveBeenCalledWith('/login');
    });
  });

  describe('hasPermission', () => {
    it('returns true when user has permission', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.PLATFORM_ADMIN,
        permissions: [Permission.ORG_READ, Permission.ORG_CREATE],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('valid-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(false);
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isAuthenticated).toBe(true);
      });

      expect(result.current.hasPermission(Permission.ORG_READ)).toBe(true);
      expect(result.current.hasPermission(Permission.ORG_CREATE)).toBe(true);
    });

    it('returns false when user does not have permission', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.DEPT_ADMIN,
        orgId: 'org-1',
        deptId: 'dept-1',
        permissions: [Permission.BUDGET_READ],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('valid-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(false);
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isAuthenticated).toBe(true);
      });

      expect(result.current.hasPermission(Permission.ORG_CREATE)).toBe(false);
      expect(result.current.hasPermission(Permission.POOL_MANAGE)).toBe(false);
    });

    it('returns false when no user', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue(null);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.hasPermission(Permission.ORG_READ)).toBe(false);
    });
  });

  describe('hasRole', () => {
    it('returns true when user has matching role', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.ORG_ADMIN,
        orgId: 'org-1',
        permissions: [Permission.ORG_READ],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('valid-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(false);
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isAuthenticated).toBe(true);
      });

      expect(result.current.hasRole(AdminRole.ORG_ADMIN)).toBe(true);
    });

    it('returns false when user has different role', async () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.DEPT_ADMIN,
        orgId: 'org-1',
        deptId: 'dept-1',
        permissions: [Permission.BUDGET_READ],
        createdAt: '2024-01-01T00:00:00Z',
      };

      vi.mocked(authService.getAccessToken).mockReturnValue('valid-token');
      vi.mocked(authService.isTokenExpired).mockReturnValue(false);
      vi.mocked(authService.getCurrentUserFromToken).mockReturnValue(mockUser);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isAuthenticated).toBe(true);
      });

      expect(result.current.hasRole(AdminRole.PLATFORM_ADMIN)).toBe(false);
      expect(result.current.hasRole(AdminRole.ORG_ADMIN)).toBe(false);
    });

    it('returns false when no user', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue(null);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.hasRole(AdminRole.PLATFORM_ADMIN)).toBe(false);
    });
  });

  describe('setAuthState', () => {
    it('updates auth state directly', async () => {
      vi.mocked(authService.getAccessToken).mockReturnValue(null);

      const { result } = renderHook(() => useAuthContext(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      const newUser = {
        id: 'new-user',
        role: AdminRole.ORG_ADMIN,
        orgId: 'org-123',
        permissions: [Permission.ORG_READ],
        createdAt: '2024-01-01T00:00:00Z',
      };

      act(() => {
        result.current.setAuthState({
          user: newUser,
          token: 'new-token',
          isAuthenticated: true,
          isLoading: false,
        });
      });

      expect(result.current.user).toEqual(newUser);
      expect(result.current.token).toBe('new-token');
      expect(result.current.isAuthenticated).toBe(true);
    });
  });

  describe('useAuthContext outside provider', () => {
    it('throws error when used outside AuthProvider', () => {
      // Suppress console.error for this test
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

      expect(() => {
        renderHook(() => useAuthContext());
      }).toThrow('useAuthContext must be used within an AuthProvider');

      consoleSpy.mockRestore();
    });
  });
});
