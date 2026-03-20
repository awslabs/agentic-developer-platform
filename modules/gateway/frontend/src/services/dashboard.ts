import { apiClient } from './api';
import type { PlatformDashboard, OrgDashboard, SystemMetrics, BudgetStatus } from '@/types';

export async function getPlatformDashboard(): Promise<PlatformDashboard> {
  const response = await apiClient.get<{
    total_organizations: number;
    total_requests_24h: number;
    total_tokens_24h: number;
    total_cost_24h: number;
    active_users_24h: number;
    error_rate_24h: number;
    pool_status: {
      total_accounts: number;
      healthy_accounts: number;
      unhealthy_accounts: number;
      accounts: Array<{
        id: string;
        account_id: string;
        role_arn: string;
        region: string;
        is_healthy: boolean;
        last_health_check: string | null;
        created_at: string;
      }>;
    };
    top_organizations: Array<{
      id: string;
      name: string;
      request_count: number;
      token_count: number;
      cost_usd: number;
    }>;
  }>('/admin/dashboard/platform');

  return {
    totalOrganizations: response.total_organizations,
    totalRequests24h: response.total_requests_24h,
    totalTokens24h: response.total_tokens_24h,
    totalCost24h: response.total_cost_24h,
    activeUsers24h: response.active_users_24h,
    errorRate24h: response.error_rate_24h,
    poolStatus: {
      totalAccounts: response.pool_status?.total_accounts ?? 0,
      healthyAccounts: response.pool_status?.healthy_accounts ?? 0,
      unhealthyAccounts: response.pool_status?.unhealthy_accounts ?? 0,
      accounts: (response.pool_status?.accounts ?? []).map((acc) => ({
        id: acc.id,
        accountId: acc.account_id,
        roleArn: acc.role_arn,
        region: acc.region,
        isHealthy: acc.is_healthy,
        lastHealthCheck: acc.last_health_check,
        createdAt: acc.created_at,
      })),
    },
    topOrganizations: response.top_organizations.map((org) => ({
      id: org.id,
      name: org.name,
      requestCount: org.request_count,
      tokenCount: org.token_count,
      costUsd: org.cost_usd,
    })),
  };
}

export async function getOrgDashboard(orgId: string): Promise<OrgDashboard> {
  const response = await apiClient.get<{
    org_id: string;
    org_name: string;
    total_requests_24h: number;
    total_tokens_24h: number;
    total_cost_24h: number;
    active_users_24h: number;
    error_rate_24h: number;
    budget_status: {
      budget_amount_usd?: number;
      current_spend_usd?: number;
      remaining_budget_usd?: number;
      budget_utilization_percent?: number;
      period_start?: string;
      period_end?: string;
      period_type?: string;
      enforcement_mode?: string;
      budget_exceeded?: boolean;
      warnings?: string[];
    };
    top_departments: Array<{
      id: string;
      name: string;
      request_count: number;
      token_count: number;
      cost_usd: number;
    }>;
    top_models: Array<{
      name: string;
      request_count: number;
      token_count: number;
      cost_usd: number;
    }>;
  }>(`/admin/dashboard/org/${orgId}`);

  return {
    orgId: response.org_id,
    orgName: response.org_name,
    totalRequests24h: response.total_requests_24h,
    totalTokens24h: response.total_tokens_24h,
    totalCost24h: response.total_cost_24h,
    activeUsers24h: response.active_users_24h,
    errorRate24h: response.error_rate_24h,
    budgetStatus: response.budget_status && response.budget_status.budget_amount_usd !== undefined
      ? transformBudgetStatus(response.budget_status as Parameters<typeof transformBudgetStatus>[0])
      : undefined,
    topDepartments: response.top_departments.map((dept) => ({
      id: dept.id,
      name: dept.name,
      requestCount: dept.request_count,
      tokenCount: dept.token_count,
      costUsd: dept.cost_usd,
    })),
    topModels: response.top_models.map((model) => ({
      name: model.name,
      requestCount: model.request_count,
      tokenCount: model.token_count,
      costUsd: model.cost_usd,
    })),
  };
}

export async function getSystemMetrics(): Promise<SystemMetrics> {
  const response = await apiClient.get<{
    api_calls_per_minute: number;
    average_latency_ms: number;
    error_rate: number;
    active_connections: number;
    cpu_utilization: number;
    memory_utilization: number;
  }>('/admin/metrics/system');

  return {
    apiCallsPerMinute: response.api_calls_per_minute,
    averageLatencyMs: response.average_latency_ms,
    errorRate: response.error_rate,
    activeConnections: response.active_connections,
    cpuUtilization: response.cpu_utilization,
    memoryUtilization: response.memory_utilization,
  };
}

function transformBudgetStatus(data: {
  budget_amount_usd: number;
  current_spend_usd: number;
  remaining_budget_usd: number;
  budget_utilization_percent: number;
  period_start: string;
  period_end: string;
  period_type: string;
  enforcement_mode: string;
  budget_exceeded: boolean;
  warnings: string[];
}): BudgetStatus {
  return {
    budgetAmountUsd: data.budget_amount_usd,
    currentSpendUsd: data.current_spend_usd,
    remainingBudgetUsd: data.remaining_budget_usd,
    budgetUtilizationPercent: data.budget_utilization_percent,
    periodStart: data.period_start,
    periodEnd: data.period_end,
    periodType: data.period_type as BudgetStatus['periodType'],
    enforcementMode: data.enforcement_mode as BudgetStatus['enforcementMode'],
    budgetExceeded: data.budget_exceeded,
    warnings: data.warnings,
  };
}
