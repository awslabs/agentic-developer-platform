import { useCallback } from 'react';
import { useAuth } from './useAuth';
import { Permission, AdminRole } from '@/types';

export function usePermissions() {
  const { user, hasPermission, hasRole } = useAuth();

  const canViewOrganizations = useCallback(
    () => hasPermission(Permission.ORG_READ),
    [hasPermission]
  );

  const canCreateOrganizations = useCallback(
    () => hasPermission(Permission.ORG_CREATE),
    [hasPermission]
  );

  const canUpdateOrganizations = useCallback(
    () => hasPermission(Permission.ORG_UPDATE),
    [hasPermission]
  );

  const canDeleteOrganizations = useCallback(
    () => hasPermission(Permission.ORG_DELETE),
    [hasPermission]
  );

  const canViewBudgets = useCallback(
    () => hasPermission(Permission.BUDGET_READ),
    [hasPermission]
  );

  const canUpdateBudgets = useCallback(
    () => hasPermission(Permission.BUDGET_UPDATE),
    [hasPermission]
  );

  const canViewRateLimits = useCallback(
    () => hasPermission(Permission.RATELIMIT_READ),
    [hasPermission]
  );

  const canUpdateRateLimits = useCallback(
    () => hasPermission(Permission.RATELIMIT_UPDATE),
    [hasPermission]
  );

  const canViewPool = useCallback(
    () => hasPermission(Permission.POOL_READ),
    [hasPermission]
  );

  const canManagePool = useCallback(
    () => hasPermission(Permission.POOL_MANAGE),
    [hasPermission]
  );

  const canViewUsage = useCallback(
    () => hasPermission(Permission.USAGE_READ),
    [hasPermission]
  );

  const canViewLogs = useCallback(
    () => hasPermission(Permission.LOGS_READ),
    [hasPermission]
  );

  const canExportLogs = useCallback(
    () => hasPermission(Permission.LOGS_EXPORT),
    [hasPermission]
  );

  const canViewUsers = useCallback(
    () => hasPermission(Permission.USER_READ),
    [hasPermission]
  );

  const canManageUsers = useCallback(
    () => hasPermission(Permission.USER_MANAGE),
    [hasPermission]
  );

  const canViewMetrics = useCallback(
    () => hasPermission(Permission.METRICS_READ),
    [hasPermission]
  );

  const isPlatformAdmin = useCallback(
    () => hasRole(AdminRole.PLATFORM_ADMIN),
    [hasRole]
  );

  const isOrgAdmin = useCallback(
    () => hasRole(AdminRole.ORG_ADMIN),
    [hasRole]
  );

  const isDeptAdmin = useCallback(
    () => hasRole(AdminRole.DEPT_ADMIN),
    [hasRole]
  );

  // Check if user can access a specific organization
  const canAccessOrg = useCallback(
    (orgId: string): boolean => {
      if (!user) return false;
      if (isPlatformAdmin()) return true;
      if ((isOrgAdmin() || isDeptAdmin()) && user.orgId === orgId) return true;
      return false;
    },
    [user, isPlatformAdmin, isOrgAdmin, isDeptAdmin]
  );

  // Check if user can access a specific department
  const canAccessDept = useCallback(
    (orgId: string, deptId: string): boolean => {
      if (!user) return false;
      if (isPlatformAdmin()) return true;
      if (isOrgAdmin() && user.orgId === orgId) return true;
      if (isDeptAdmin() && user.orgId === orgId && user.deptId === deptId) return true;
      return false;
    },
    [user, isPlatformAdmin, isOrgAdmin, isDeptAdmin]
  );

  return {
    user,
    hasPermission,
    hasRole,
    canViewOrganizations,
    canCreateOrganizations,
    canUpdateOrganizations,
    canDeleteOrganizations,
    canViewBudgets,
    canUpdateBudgets,
    canViewRateLimits,
    canUpdateRateLimits,
    canViewPool,
    canManagePool,
    canViewUsage,
    canViewLogs,
    canExportLogs,
    canViewUsers,
    canManageUsers,
    canViewMetrics,
    isPlatformAdmin,
    isOrgAdmin,
    isDeptAdmin,
    canAccessOrg,
    canAccessDept,
  };
}
