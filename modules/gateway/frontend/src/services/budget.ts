import { apiClient, buildQueryString } from './api';
import type { PaginatedResponse } from '@/types/api';
import type {
  Budget,
  BudgetStatus,
  BudgetUsage,
  BudgetCreateRequest,
  BudgetUpdateRequest,
  EntityType,
  PeriodType,
  EnforcementMode,
  UsageDataPoint,
} from '@/types';

// Budget list item with utilization data (Issue #185)
export interface BudgetListItem {
  entityType: EntityType;
  entityId: string;
  entityDisplayName: string | null;
  periodType: PeriodType;
  budgetAmountUsd: number;
  enforcementMode: EnforcementMode;
  currentUsageUsd: number;
  utilizationPct: number;
  updatedAt: string;
}

// Budget configuration endpoints
export async function getBudgets(
  orgId: string,
  params?: {
    entityType?: EntityType;
    page?: number;
    pageSize?: number;
  }
): Promise<PaginatedResponse<Budget>> {
  const query = buildQueryString({
    entity_type: params?.entityType,
    page: params?.page || 1,
    limit: params?.pageSize || 50,
  });

  const response = await apiClient.get<{
    items: Array<{
      id: string;
      entity_type: string;
      entity_id: string;
      period_type: string;
      budget_amount_usd: number;
      enforcement_mode: string;
      org_id: string;
      updated_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/budgets${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformBudget),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

// Get budgets list with utilization data (Issue #185)
export async function getBudgetsWithUtilization(
  orgId: string,
  params?: {
    entityType?: EntityType;
    page?: number;
    limit?: number;
  }
): Promise<PaginatedResponse<BudgetListItem>> {
  const query = buildQueryString({
    entity_type: params?.entityType,
    page: params?.page || 1,
    limit: params?.limit || 20,
  });

  const response = await apiClient.get<{
    items: Array<{
      entity_type: string;
      entity_id: string;
      entity_display_name: string | null;
      period_type: string;
      budget_amount_usd: number;
      enforcement_mode: string;
      current_usage_usd: number;
      utilization_pct: number;
      updated_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/budgets${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((item) => ({
      entityType: item.entity_type as EntityType,
      entityId: item.entity_id,
      entityDisplayName: item.entity_display_name,
      periodType: item.period_type as PeriodType,
      budgetAmountUsd: item.budget_amount_usd,
      enforcementMode: item.enforcement_mode as EnforcementMode,
      currentUsageUsd: item.current_usage_usd,
      utilizationPct: item.utilization_pct,
      updatedAt: item.updated_at,
    })),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 20,
    hasMore: response?.has_more ?? false,
  };
}

export async function getBudgetStatus(
  orgId: string,
  entityType: EntityType,
  entityId: string
): Promise<BudgetStatus> {
  const response = await apiClient.get<{
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
  }>(`/admin/organizations/${orgId}/budgets/${entityType}/${entityId}/status`);

  return {
    budgetAmountUsd: response.budget_amount_usd,
    currentSpendUsd: response.current_spend_usd,
    remainingBudgetUsd: response.remaining_budget_usd,
    budgetUtilizationPercent: response.budget_utilization_percent,
    periodStart: response.period_start,
    periodEnd: response.period_end,
    periodType: response.period_type as PeriodType,
    enforcementMode: response.enforcement_mode as EnforcementMode,
    budgetExceeded: response.budget_exceeded,
    warnings: response.warnings,
  };
}

export async function createBudget(orgId: string, data: BudgetCreateRequest): Promise<Budget> {
  const response = await apiClient.post<{
    org_id: string;
    entity_type: string;
    entity_id: string;
    period_type: string;
    budget_amount_usd: number;
    enforcement_mode: string;
    updated_at: string;
  }>(`/admin/organizations/${orgId}/budgets`, data);
  // Transform the response - note: this endpoint returns BudgetConfigResponse
  return {
    id: `${response.entity_type}-${response.entity_id}-${response.period_type}`,
    entityType: response.entity_type as EntityType,
    entityId: response.entity_id,
    periodType: response.period_type as PeriodType,
    budgetAmountUsd: response.budget_amount_usd,
    enforcementMode: response.enforcement_mode as EnforcementMode,
    orgId: response.org_id,
    updatedAt: response.updated_at,
  };
}

export async function updateBudget(
  orgId: string,
  entityType: string,
  entityId: string,
  data: BudgetUpdateRequest
): Promise<Budget> {
  const response = await apiClient.put<{
    org_id: string;
    entity_type: string;
    entity_id: string;
    period_type: string;
    budget_amount_usd: number;
    enforcement_mode: string;
    updated_at: string;
  }>(`/admin/organizations/${orgId}/budget/${entityType}/${entityId}`, data);
  // Transform the response - note: this endpoint returns BudgetConfigResponse
  return {
    id: `${response.entity_type}-${response.entity_id}-${response.period_type}`,
    entityType: response.entity_type as EntityType,
    entityId: response.entity_id,
    periodType: response.period_type as PeriodType,
    budgetAmountUsd: response.budget_amount_usd,
    enforcementMode: response.enforcement_mode as EnforcementMode,
    orgId: response.org_id,
    updatedAt: response.updated_at,
  };
}

export async function deleteBudget(
  orgId: string,
  entityType: string,
  entityId: string,
  periodType: string
): Promise<void> {
  await apiClient.delete(`/admin/organizations/${orgId}/budget/${entityType}/${entityId}/${periodType}`);
}

/**
 * Delete budget by composite ID (entity_type-entity_id-period_type).
 * Used by DepartmentDashboard where budget.id is a composite string.
 */
export async function deleteBudgetById(orgId: string, budgetId: string): Promise<void> {
  // budgetId format: "entity_type-entity_id-period_type"
  const parts = budgetId.split('-');
  if (parts.length < 3) {
    throw new Error(`Invalid budget ID format: ${budgetId}`);
  }
  const entityType = parts[0];
  const periodType = parts[parts.length - 1];
  // entity_id may contain dashes, so join the middle parts
  const entityId = parts.slice(1, -1).join('-');
  await apiClient.delete(`/admin/organizations/${orgId}/budget/${entityType}/${entityId}/${periodType}`);
}

// Delete budget by entity and period type (Issue #185)
export async function deleteBudgetByEntity(
  orgId: string,
  entityType: string,
  entityId: string,
  periodType: string
): Promise<void> {
  await apiClient.delete(`/admin/organizations/${orgId}/budget/${entityType}/${entityId}/${periodType}`);
}

// Usage endpoints
export async function getUsage(
  orgId: string,
  params?: {
    entityType?: EntityType;
    entityId?: string;
    startDate?: string;
    endDate?: string;
    page?: number;
    pageSize?: number;
  }
): Promise<PaginatedResponse<BudgetUsage>> {
  const query = buildQueryString({
    entity_type: params?.entityType,
    entity_id: params?.entityId,
    start_date: params?.startDate,
    end_date: params?.endDate,
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });

  const response = await apiClient.get<{
    items: Array<{
      id: string;
      entity_type: string;
      entity_id: string;
      period_start: string;
      period_type: string;
      total_cost_usd: number;
      total_tokens: number;
      request_count: number;
      org_id: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/usage${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformBudgetUsage),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

export async function getUsageTimeSeries(
  orgId: string,
  params: {
    entityType?: EntityType;
    entityId?: string;
    startDate: string;
    endDate: string;
    granularity?: 'hour' | 'day' | 'week';
  }
): Promise<UsageDataPoint[]> {
  // Map granularity to backend period parameter
  const periodMap: Record<string, string> = {
    hour: 'daily',
    day: 'daily',
    week: 'weekly',
  };
  const period = periodMap[params.granularity || 'day'] || 'daily';

  const query = buildQueryString({
    period,
    start: params.startDate?.split('T')[0], // Convert ISO to YYYY-MM-DD
    end: params.endDate?.split('T')[0], // Convert ISO to YYYY-MM-DD
  });

  const response = await apiClient.get<{
    data: Array<{
      date: string;
      input_tokens: number;
      output_tokens: number;
      cost_usd: number;
      request_count: number;
    }>;
    period: string;
    org_id: string;
  }>(`/admin/organizations/${orgId}/usage/timeseries${query}`);

  // Transform backend response to frontend format
  const data = Array.isArray(response?.data) ? response.data : [];
  return data.map((item) => ({
    timestamp: item.date,
    requestCount: item.request_count,
    tokenCount: item.input_tokens + item.output_tokens,
    costUsd: typeof item.cost_usd === 'string' ? parseFloat(item.cost_usd) : item.cost_usd,
    errorCount: 0, // Backend doesn't track errors in timeseries currently
  }));
}

// Transform functions
function transformBudget(data: {
  id: string;
  entity_type: string;
  entity_id: string;
  period_type: string;
  budget_amount_usd: number;
  enforcement_mode: string;
  org_id: string;
  updated_at: string;
}): Budget {
  return {
    id: data.id,
    entityType: data.entity_type as EntityType,
    entityId: data.entity_id,
    periodType: data.period_type as PeriodType,
    budgetAmountUsd: data.budget_amount_usd,
    enforcementMode: data.enforcement_mode as EnforcementMode,
    orgId: data.org_id,
    updatedAt: data.updated_at,
  };
}

function transformBudgetUsage(data: {
  id: string;
  entity_type: string;
  entity_id: string;
  period_start: string;
  period_type: string;
  total_cost_usd: number;
  total_tokens: number;
  request_count: number;
  org_id: string;
}): BudgetUsage {
  return {
    id: data.id,
    entityType: data.entity_type as EntityType,
    entityId: data.entity_id,
    periodStart: data.period_start,
    periodType: data.period_type as PeriodType,
    totalCostUsd: data.total_cost_usd,
    totalTokens: data.total_tokens,
    requestCount: data.request_count,
    orgId: data.org_id,
  };
}
