import { useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Tabs, TabsList, Tab, TabPanel, Alert } from '@/components/ui';
import { StatCard } from '@/components/dashboard/StatCard';
import { DepartmentList } from '@/components/org/DepartmentList';
import { UserList } from '@/components/org/UserList';
import { BudgetOverview } from '@/components/org/BudgetOverview';
import { UsageChart } from '@/components/org/UsageChart';
import { ApprovalPolicyToggle } from '@/components/org/ApprovalPolicyToggle';
import { CardSkeleton, TableSkeleton } from '@/components/LoadingScreen';
import { getOrgDashboard } from '@/services/dashboard';
import {
  getDepartments,
  getUserRoles,
  getOrganization,
  getAvailableRoles,
  assignUserRole,
  removeUserRole,
} from '@/services/admin';
import { getUsageTimeSeries } from '@/services/budget';
import { formatCurrency, formatNumber, formatPercent } from '@/utils/format';
import { usePermissions } from '@/hooks/usePermissions';
import { useToast } from '@/contexts/ToastContext';
import { AdminRole, type UserRole } from '@/types';

/**
 * The API client throws the parsed error body (a plain object with `message`),
 * not an `Error`, so `error instanceof Error` misses the useful text — which is
 * exactly the 403 reason ("Cannot change your own role", "Cannot assign a role
 * above your own") that an admin needs to see.
 */
function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) return error.message;
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string' && message) return message;
  }
  return fallback;
}

export default function OrgDashboard() {
  const { orgId } = useParams<{ orgId: string }>();
  const { canManageUsers, canViewBudgets, canViewUsage, canAccessOrg } = usePermissions();
  const queryClient = useQueryClient();
  const toast = useToast();

  // Check access
  if (orgId && !canAccessOrg(orgId)) {
    return (
      <Alert variant="error" title="Access Denied">
        You don't have permission to view this organization.
      </Alert>
    );
  }

  const {
    data: dashboard,
    isLoading: isDashboardLoading,
    error: dashboardError,
  } = useQuery({
    queryKey: ['orgDashboard', orgId],
    queryFn: () => getOrgDashboard(orgId!),
    enabled: !!orgId,
    refetchInterval: 60000,
  });

  const { data: departments, isLoading: isDepartmentsLoading } = useQuery({
    queryKey: ['departments', orgId],
    queryFn: () => getDepartments(orgId!),
    enabled: !!orgId,
  });

  const { data: users, isLoading: isUsersLoading } = useQuery({
    queryKey: ['userRoles', orgId],
    queryFn: () => getUserRoles(orgId),
    enabled: !!orgId && canManageUsers(),
  });

  // Issue #4019: the roles the CURRENT CALLER may assign. Backend-filtered, so
  // an org admin is never offered `platform_admin` (submitting it would 403).
  const { data: availableRoles } = useQuery({
    queryKey: ['availableRoles'],
    queryFn: getAvailableRoles,
    enabled: canManageUsers(),
  });

  const changeRoleMutation = useMutation({
    mutationFn: ({ user, role }: { user: UserRole; role: string }) =>
      assignUserRole({ user_id: user.userId, role: role as AdminRole, org_id: orgId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['userRoles', orgId] });
      toast.success('Role updated');
    },
    onError: (error) => {
      toast.error(errorMessage(error, 'Failed to update role'));
    },
  });

  const removeRoleMutation = useMutation({
    mutationFn: (user: UserRole) => removeUserRole(user.userId, orgId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['userRoles', orgId] });
      toast.success('Role removed — user is now a member');
    },
    onError: (error) => {
      toast.error(errorMessage(error, 'Failed to remove role'));
    },
  });

  const { data: usageData } = useQuery({
    queryKey: ['usageTimeSeries', orgId],
    queryFn: () =>
      getUsageTimeSeries(orgId!, {
        startDate: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(),
        endDate: new Date().toISOString(),
        granularity: 'day',
      }),
    enabled: !!orgId && canViewUsage(),
  });

  // Issue #2984: Fetch org detail to get member_approval_policy for the toggle
  const { data: orgDetail } = useQuery({
    queryKey: ['organization', orgId],
    queryFn: () => getOrganization(orgId!),
    enabled: !!orgId && canManageUsers(),
  });

  if (dashboardError) {
    return (
      <Alert variant="error" title="Error loading organization">
        {dashboardError instanceof Error ? dashboardError.message : 'Failed to load organization data'}
      </Alert>
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          {dashboard?.orgName || 'Organization Dashboard'}
        </h1>
        <p className="text-gray-500 dark:text-gray-400 mt-1">
          Manage departments, users, and view usage for this organization
        </p>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {isDashboardLoading ? (
          <>
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
            <CardSkeleton />
          </>
        ) : dashboard ? (
          <>
            <StatCard
              title="Requests (24h)"
              value={formatNumber(dashboard.totalRequests24h)}
              icon="📊"
            />
            <StatCard
              title="Tokens (24h)"
              value={formatNumber(dashboard.totalTokens24h)}
              icon="🔤"
            />
            <StatCard
              title="Cost (24h)"
              value={formatCurrency(dashboard.totalCost24h)}
              icon="💰"
            />
            <StatCard
              title="Error Rate (24h)"
              value={formatPercent(dashboard.errorRate24h)}
              icon={dashboard.errorRate24h > 5 ? '⚠️' : '✅'}
            />
          </>
        ) : null}
      </div>

      {/* Budget and Usage */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {canViewBudgets() && dashboard?.budgetStatus && dashboard.budgetStatus.budgetAmountUsd > 0 && (
          <BudgetOverview budgetStatus={dashboard.budgetStatus} />
        )}

        {canViewUsage() && usageData && usageData.length > 0 && (
          <UsageChart data={usageData} title="Usage (Last 7 Days)" />
        )}
      </div>

      {/* Tabbed Content */}
      <Tabs defaultValue="departments">
        <TabsList>
          <Tab value="departments">Departments</Tab>
          {canManageUsers() && <Tab value="users">Admin Users</Tab>}
          {dashboard?.topModels && <Tab value="models">Top Models</Tab>}
        </TabsList>

        <TabPanel value="departments">
          {isDepartmentsLoading ? (
            <TableSkeleton />
          ) : departments ? (
            <DepartmentList
              orgId={orgId!}
              departments={departments.items}
              topDepartments={dashboard?.topDepartments}
              canManage={canManageUsers()}
            />
          ) : null}
        </TabPanel>

        {canManageUsers() && (
          <TabPanel value="users">
            {isUsersLoading ? (
              <TableSkeleton />
            ) : users ? (
              <UserList
                users={users.items}
                canManage={canManageUsers()}
                availableRoles={availableRoles}
                onChangeRole={(user, role) => changeRoleMutation.mutateAsync({ user, role })}
                onRemoveRole={(user) => removeRoleMutation.mutateAsync(user)}
              />
            ) : null}
          </TabPanel>
        )}

        {dashboard?.topModels && (
          <TabPanel value="models">
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
              <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead className="bg-gray-50 dark:bg-gray-800">
                  <tr>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                      Model
                    </th>
                    <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">
                      Requests
                    </th>
                    <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">
                      Tokens
                    </th>
                    <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">
                      Cost
                    </th>
                  </tr>
                </thead>
                <tbody className="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-700">
                  {dashboard.topModels.map((model) => (
                    <tr key={model.name}>
                      <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-white">
                        {model.name}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 text-right">
                        {formatNumber(model.requestCount)}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 text-right">
                        {formatNumber(model.tokenCount)}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 text-right">
                        {formatCurrency(model.costUsd)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </TabPanel>
        )}
      </Tabs>

      {/* Issue #2984: Org settings — approval policy toggle */}
      {canManageUsers() && orgDetail?.memberApprovalPolicy && (
        <div className="space-y-3">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Organization Settings
          </h2>
          <ApprovalPolicyToggle
            orgId={orgId!}
            currentPolicy={orgDetail.memberApprovalPolicy as 'auto_approve_org_members' | 'require_admin_approval'}
            canManage={canManageUsers()}
          />
        </div>
      )}
    </div>
  );
}
