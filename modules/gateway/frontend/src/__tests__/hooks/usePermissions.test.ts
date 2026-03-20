import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { usePermissions } from '@/hooks/usePermissions';
import { Permission, AdminRole } from '@/types';

// Mock useAuth hook
const mockUseAuth = vi.fn();

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

describe('usePermissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Permission checks', () => {
    it('returns correct permissions for platform admin', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-1',
          role: AdminRole.PLATFORM_ADMIN,
          permissions: [
            Permission.ORG_CREATE,
            Permission.ORG_READ,
            Permission.ORG_UPDATE,
            Permission.ORG_DELETE,
            Permission.POOL_READ,
            Permission.POOL_MANAGE,
            Permission.BUDGET_READ,
            Permission.BUDGET_UPDATE,
            Permission.LOGS_READ,
            Permission.LOGS_EXPORT,
            Permission.USER_READ,
            Permission.USER_MANAGE,
            Permission.METRICS_READ,
          ],
        },
        hasPermission: (perm: Permission) =>
          [
            Permission.ORG_CREATE,
            Permission.ORG_READ,
            Permission.ORG_UPDATE,
            Permission.ORG_DELETE,
            Permission.POOL_READ,
            Permission.POOL_MANAGE,
            Permission.BUDGET_READ,
            Permission.BUDGET_UPDATE,
            Permission.LOGS_READ,
            Permission.LOGS_EXPORT,
            Permission.USER_READ,
            Permission.USER_MANAGE,
            Permission.METRICS_READ,
          ].includes(perm),
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canViewOrganizations()).toBe(true);
      expect(result.current.canCreateOrganizations()).toBe(true);
      expect(result.current.canUpdateOrganizations()).toBe(true);
      expect(result.current.canDeleteOrganizations()).toBe(true);
      expect(result.current.canViewPool()).toBe(true);
      expect(result.current.canManagePool()).toBe(true);
      expect(result.current.canViewBudgets()).toBe(true);
      expect(result.current.canUpdateBudgets()).toBe(true);
      expect(result.current.canViewMetrics()).toBe(true);
    });

    it('returns correct permissions for org admin', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-2',
          role: AdminRole.ORG_ADMIN,
          orgId: 'org-1',
          permissions: [
            Permission.ORG_READ,
            Permission.ORG_UPDATE,
            Permission.BUDGET_READ,
            Permission.BUDGET_UPDATE,
            Permission.LOGS_READ,
            Permission.LOGS_EXPORT,
            Permission.USER_READ,
            Permission.USER_MANAGE,
          ],
        },
        hasPermission: (perm: Permission) =>
          [
            Permission.ORG_READ,
            Permission.ORG_UPDATE,
            Permission.BUDGET_READ,
            Permission.BUDGET_UPDATE,
            Permission.LOGS_READ,
            Permission.LOGS_EXPORT,
            Permission.USER_READ,
            Permission.USER_MANAGE,
          ].includes(perm),
        hasRole: (role: AdminRole) => role === AdminRole.ORG_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canViewOrganizations()).toBe(true);
      expect(result.current.canCreateOrganizations()).toBe(false);
      expect(result.current.canUpdateOrganizations()).toBe(true);
      expect(result.current.canDeleteOrganizations()).toBe(false);
      expect(result.current.canViewPool()).toBe(false);
      expect(result.current.canManagePool()).toBe(false);
      expect(result.current.canViewBudgets()).toBe(true);
      expect(result.current.canUpdateBudgets()).toBe(true);
    });

    it('returns correct permissions for department admin', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-3',
          role: AdminRole.DEPT_ADMIN,
          orgId: 'org-1',
          deptId: 'dept-1',
          permissions: [
            Permission.BUDGET_READ,
            Permission.RATELIMIT_READ,
            Permission.USAGE_READ,
            Permission.LOGS_READ,
            Permission.USER_READ,
          ],
        },
        hasPermission: (perm: Permission) =>
          [
            Permission.BUDGET_READ,
            Permission.RATELIMIT_READ,
            Permission.USAGE_READ,
            Permission.LOGS_READ,
            Permission.USER_READ,
          ].includes(perm),
        hasRole: (role: AdminRole) => role === AdminRole.DEPT_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canViewOrganizations()).toBe(false);
      expect(result.current.canCreateOrganizations()).toBe(false);
      expect(result.current.canViewBudgets()).toBe(true);
      expect(result.current.canUpdateBudgets()).toBe(false);
      expect(result.current.canViewUsage()).toBe(true);
      expect(result.current.canViewLogs()).toBe(true);
      expect(result.current.canExportLogs()).toBe(false);
      expect(result.current.canViewUsers()).toBe(true);
      expect(result.current.canManageUsers()).toBe(false);
    });
  });

  describe('Role checks', () => {
    it('isPlatformAdmin returns true for platform admin', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', role: AdminRole.PLATFORM_ADMIN, permissions: [] },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.isPlatformAdmin()).toBe(true);
      expect(result.current.isOrgAdmin()).toBe(false);
      expect(result.current.isDeptAdmin()).toBe(false);
    });

    it('isOrgAdmin returns true for org admin', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-2', role: AdminRole.ORG_ADMIN, orgId: 'org-1', permissions: [] },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.ORG_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.isPlatformAdmin()).toBe(false);
      expect(result.current.isOrgAdmin()).toBe(true);
      expect(result.current.isDeptAdmin()).toBe(false);
    });

    it('isDeptAdmin returns true for department admin', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-3',
          role: AdminRole.DEPT_ADMIN,
          orgId: 'org-1',
          deptId: 'dept-1',
          permissions: [],
        },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.DEPT_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.isPlatformAdmin()).toBe(false);
      expect(result.current.isOrgAdmin()).toBe(false);
      expect(result.current.isDeptAdmin()).toBe(true);
    });
  });

  describe('Organization access checks', () => {
    it('canAccessOrg returns true for platform admin for any org', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', role: AdminRole.PLATFORM_ADMIN, permissions: [] },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessOrg('org-1')).toBe(true);
      expect(result.current.canAccessOrg('org-2')).toBe(true);
      expect(result.current.canAccessOrg('any-org')).toBe(true);
    });

    it('canAccessOrg returns true for org admin for their org', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-2', role: AdminRole.ORG_ADMIN, orgId: 'org-1', permissions: [] },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.ORG_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessOrg('org-1')).toBe(true);
      expect(result.current.canAccessOrg('org-2')).toBe(false);
    });

    it('canAccessOrg returns true for dept admin for their org', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-3',
          role: AdminRole.DEPT_ADMIN,
          orgId: 'org-1',
          deptId: 'dept-1',
          permissions: [],
        },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.DEPT_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessOrg('org-1')).toBe(true);
      expect(result.current.canAccessOrg('org-2')).toBe(false);
    });

    it('canAccessOrg returns false when no user', () => {
      mockUseAuth.mockReturnValue({
        user: null,
        hasPermission: () => false,
        hasRole: () => false,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessOrg('org-1')).toBe(false);
    });
  });

  describe('Department access checks', () => {
    it('canAccessDept returns true for platform admin for any dept', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', role: AdminRole.PLATFORM_ADMIN, permissions: [] },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessDept('org-1', 'dept-1')).toBe(true);
      expect(result.current.canAccessDept('org-2', 'dept-2')).toBe(true);
    });

    it('canAccessDept returns true for org admin for depts in their org', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-2', role: AdminRole.ORG_ADMIN, orgId: 'org-1', permissions: [] },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.ORG_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessDept('org-1', 'dept-1')).toBe(true);
      expect(result.current.canAccessDept('org-1', 'dept-2')).toBe(true);
      expect(result.current.canAccessDept('org-2', 'dept-1')).toBe(false);
    });

    it('canAccessDept returns true for dept admin only for their dept', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-3',
          role: AdminRole.DEPT_ADMIN,
          orgId: 'org-1',
          deptId: 'dept-1',
          permissions: [],
        },
        hasPermission: () => false,
        hasRole: (role: AdminRole) => role === AdminRole.DEPT_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessDept('org-1', 'dept-1')).toBe(true);
      expect(result.current.canAccessDept('org-1', 'dept-2')).toBe(false);
      expect(result.current.canAccessDept('org-2', 'dept-1')).toBe(false);
    });

    it('canAccessDept returns false when no user', () => {
      mockUseAuth.mockReturnValue({
        user: null,
        hasPermission: () => false,
        hasRole: () => false,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canAccessDept('org-1', 'dept-1')).toBe(false);
    });
  });

  describe('Rate limit permissions', () => {
    it('platform admin can view and update rate limits', () => {
      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', role: AdminRole.PLATFORM_ADMIN, permissions: [] },
        hasPermission: (perm: Permission) =>
          [Permission.RATELIMIT_READ, Permission.RATELIMIT_UPDATE].includes(perm),
        hasRole: (role: AdminRole) => role === AdminRole.PLATFORM_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canViewRateLimits()).toBe(true);
      expect(result.current.canUpdateRateLimits()).toBe(true);
    });

    it('dept admin can only view rate limits', () => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 'user-3',
          role: AdminRole.DEPT_ADMIN,
          orgId: 'org-1',
          deptId: 'dept-1',
          permissions: [],
        },
        hasPermission: (perm: Permission) => perm === Permission.RATELIMIT_READ,
        hasRole: (role: AdminRole) => role === AdminRole.DEPT_ADMIN,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.canViewRateLimits()).toBe(true);
      expect(result.current.canUpdateRateLimits()).toBe(false);
    });
  });

  describe('Returned values', () => {
    it('returns user from context', () => {
      const mockUser = {
        id: 'user-1',
        role: AdminRole.PLATFORM_ADMIN,
        permissions: [Permission.ORG_READ],
      };

      mockUseAuth.mockReturnValue({
        user: mockUser,
        hasPermission: () => true,
        hasRole: () => true,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.user).toEqual(mockUser);
    });

    it('returns hasPermission function', () => {
      const mockHasPermission = vi.fn().mockReturnValue(true);

      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', role: AdminRole.PLATFORM_ADMIN, permissions: [] },
        hasPermission: mockHasPermission,
        hasRole: () => true,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.hasPermission).toBe(mockHasPermission);
    });

    it('returns hasRole function', () => {
      const mockHasRole = vi.fn().mockReturnValue(true);

      mockUseAuth.mockReturnValue({
        user: { id: 'user-1', role: AdminRole.PLATFORM_ADMIN, permissions: [] },
        hasPermission: () => true,
        hasRole: mockHasRole,
      });

      const { result } = renderHook(() => usePermissions());

      expect(result.current.hasRole).toBe(mockHasRole);
    });
  });
});
