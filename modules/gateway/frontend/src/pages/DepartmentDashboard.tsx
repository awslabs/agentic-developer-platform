import { useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Tabs, TabsList, Tab, TabPanel, Alert } from '@/components/ui';
import { StatCard } from '@/components/dashboard/StatCard';
import { TeamManagement } from '@/components/department/TeamManagement';
import { BudgetAllocation } from '@/components/department/BudgetAllocation';
import { UsageChart } from '@/components/org/UsageChart';
import { TableSkeleton } from '@/components/LoadingScreen';
import { getTeams, createTeam, updateTeam, deleteTeam } from '@/services/admin';
import { getBudgets, createBudget, updateBudget, deleteBudgetById, getUsageTimeSeries } from '@/services/budget';
import { formatCurrency, formatNumber } from '@/utils/format';
import { usePermissions } from '@/hooks/usePermissions';
import { useToast } from '@/contexts/ToastContext';
import { EntityType } from '@/types';
import type { PeriodType, EnforcementMode } from '@/types';

export default function DepartmentDashboard() {
  const { orgId, deptId } = useParams<{ orgId: string; deptId: string }>();
  const { canAccessDept, canUpdateBudgets, canManageUsers, canViewUsage } = usePermissions();
  const queryClient = useQueryClient();
  const toast = useToast();

  // Check access
  if (orgId && deptId && !canAccessDept(orgId, deptId)) {
    return (
      <Alert variant="error" title="Access Denied">
        You don't have permission to view this department.
      </Alert>
    );
  }

  // Fetch teams
  const { data: teamsData, isLoading: isTeamsLoading } = useQuery({
    queryKey: ['teams', orgId, deptId],
    queryFn: () => getTeams(orgId!, deptId!),
    enabled: !!orgId && !!deptId,
  });

  // Fetch budgets
  const { data: budgetsData, isLoading: isBudgetsLoading } = useQuery({
    queryKey: ['budgets', orgId, deptId],
    queryFn: () => getBudgets(orgId!),
    enabled: !!orgId && canUpdateBudgets(),
  });

  // Fetch usage data
  const { data: usageData } = useQuery({
    queryKey: ['usageTimeSeries', orgId, deptId],
    queryFn: () =>
      getUsageTimeSeries(orgId!, {
        entityType: EntityType.DEPARTMENT,
        entityId: deptId,
        startDate: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(),
        endDate: new Date().toISOString(),
        granularity: 'day',
      }),
    enabled: !!orgId && !!deptId && canViewUsage(),
  });

  // Team mutations
  const createTeamMutation = useMutation({
    mutationFn: (data: { name: string; description?: string }) =>
      createTeam(orgId!, deptId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['teams', orgId, deptId] });
      toast.success('Team created successfully');
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : 'Failed to create team');
    },
  });

  const updateTeamMutation = useMutation({
    mutationFn: ({ teamId, data }: { teamId: string; data: { name?: string; description?: string } }) =>
      updateTeam(orgId!, teamId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['teams', orgId, deptId] });
      toast.success('Team updated successfully');
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : 'Failed to update team');
    },
  });

  const deleteTeamMutation = useMutation({
    mutationFn: (teamId: string) => deleteTeam(orgId!, teamId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['teams', orgId, deptId] });
      toast.success('Team deleted successfully');
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : 'Failed to delete team');
    },
  });

  // Budget mutations
  const createBudgetMutation = useMutation({
    mutationFn: (data: {
      entityType: EntityType;
      entityId: string;
      periodType: PeriodType;
      budgetAmountUsd: number;
      enforcementMode: EnforcementMode;
    }) =>
      createBudget(orgId!, {
        entity_type: data.entityType,
        entity_id: data.entityId,
        period_type: data.periodType,
        budget_amount_usd: data.budgetAmountUsd,
        enforcement_mode: data.enforcementMode,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['budgets', orgId] });
      toast.success('Budget created successfully');
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : 'Failed to create budget');
    },
  });

  const updateBudgetMutation = useMutation({
    mutationFn: ({
      entityType,
      entityId,
      data,
    }: {
      entityType: string;
      entityId: string;
      data: { budgetAmountUsd?: number; enforcementMode?: EnforcementMode };
    }) =>
      updateBudget(orgId!, entityType, entityId, {
        budget_amount_usd: data.budgetAmountUsd,
        enforcement_mode: data.enforcementMode,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['budgets', orgId] });
      toast.success('Budget updated successfully');
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : 'Failed to update budget');
    },
  });

  const deleteBudgetMutation = useMutation({
    mutationFn: (budgetId: string) => deleteBudgetById(orgId!, budgetId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['budgets', orgId] });
      toast.success('Budget deleted successfully');
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : 'Failed to delete budget');
    },
  });

  // Calculate usage stats
  const usageStats = usageData?.reduce(
    (acc, point) => ({
      requestCount: acc.requestCount + point.requestCount,
      tokenCount: acc.tokenCount + point.tokenCount,
      costUsd: acc.costUsd + point.costUsd,
    }),
    { requestCount: 0, tokenCount: 0, costUsd: 0 }
  ) || { requestCount: 0, tokenCount: 0, costUsd: 0 };

  // Build entities list for budget allocation
  const entities = [
    ...(teamsData?.items.map((t) => ({ id: t.id, name: t.name, type: 'team' as EntityType })) || []),
  ];

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Department Dashboard</h1>
        <p className="text-gray-500 dark:text-gray-400 mt-1">
          Manage teams and view usage for this department
        </p>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatCard
          title="Teams"
          value={teamsData?.total || 0}
          icon="👥"
        />
        <StatCard
          title="Requests (7d)"
          value={formatNumber(usageStats.requestCount)}
          icon="📊"
        />
        <StatCard
          title="Cost (7d)"
          value={formatCurrency(usageStats.costUsd)}
          icon="💰"
        />
      </div>

      {/* Usage Chart */}
      {canViewUsage() && usageData && usageData.length > 0 && (
        <UsageChart data={usageData} title="Department Usage (Last 7 Days)" />
      )}

      {/* Tabbed Content */}
      <Tabs defaultValue="teams">
        <TabsList>
          <Tab value="teams">Teams</Tab>
          {canUpdateBudgets() && <Tab value="budgets">Budgets</Tab>}
        </TabsList>

        <TabPanel value="teams">
          {isTeamsLoading ? (
            <TableSkeleton />
          ) : teamsData ? (
            <TeamManagement
              teams={teamsData.items}
              onCreateTeam={async (data) => {
                await createTeamMutation.mutateAsync(data);
              }}
              onUpdateTeam={async (teamId, data) => {
                await updateTeamMutation.mutateAsync({ teamId, data });
              }}
              onDeleteTeam={async (teamId) => {
                await deleteTeamMutation.mutateAsync(teamId);
              }}
              isLoading={isTeamsLoading}
              canManage={canManageUsers()}
            />
          ) : null}
        </TabPanel>

        {canUpdateBudgets() && (
          <TabPanel value="budgets">
            {isBudgetsLoading ? (
              <TableSkeleton />
            ) : budgetsData ? (
              <BudgetAllocation
                budgets={budgetsData.items.filter(
                  (b) => b.entityType === 'team' && entities.some((e) => e.id === b.entityId)
                )}
                entities={entities}
                onCreateBudget={async (data) => {
                  await createBudgetMutation.mutateAsync(data);
                }}
                onUpdateBudget={async (budget, data) => {
                  // budget is a Budget object; we need entityType and entityId
                  await updateBudgetMutation.mutateAsync({
                    entityType: budget.entityType,
                    entityId: budget.entityId,
                    data,
                  });
                }}
                onDeleteBudget={async (budgetId) => {
                  await deleteBudgetMutation.mutateAsync(budgetId);
                }}
                isLoading={isBudgetsLoading}
                canManage={canUpdateBudgets()}
              />
            ) : null}
          </TabPanel>
        )}
      </Tabs>
    </div>
  );
}
