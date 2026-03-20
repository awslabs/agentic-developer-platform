import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getBudgets,
  getBudgetStatus,
  createBudget,
  updateBudget,
  deleteBudget,
  getUsage,
  getUsageTimeSeries,
} from '@/services/budget';
import { EntityType, PeriodType, EnforcementMode } from '@/types';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  buildQueryString: vi.fn((params) => {
    const searchParams = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') {
        searchParams.append(key, String(value));
      }
    }
    const queryString = searchParams.toString();
    return queryString ? `?${queryString}` : '';
  }),
}));

describe('Budget Service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('getBudgets', () => {
    it('fetches budgets for an organization', async () => {
      const mockResponse = {
        items: [
          {
            id: 'budget-1',
            entity_type: 'org',
            entity_id: 'org-1',
            period_type: 'monthly',
            budget_amount_usd: 1000,
            enforcement_mode: 'soft',
            org_id: 'org-1',
            updated_at: '2024-01-01T00:00:00Z',
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getBudgets('org-1');

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/budgets')
      );
      expect(result.items).toHaveLength(1);
      expect(result.items[0].id).toBe('budget-1');
      expect(result.items[0].entityType).toBe(EntityType.ORGANIZATION);
      expect(result.items[0].periodType).toBe(PeriodType.MONTHLY);
      expect(result.items[0].budgetAmountUsd).toBe(1000);
    });

    it('fetches budgets with entity type filter', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getBudgets('org-1', { entityType: EntityType.DEPARTMENT });

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/budgets')
      );
    });

    it('fetches budgets with pagination', async () => {
      const mockResponse = {
        items: [],
        total: 100,
        page: 2,
        page_size: 10,
        has_more: true,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getBudgets('org-1', { page: 2, pageSize: 10 });

      expect(result.page).toBe(2);
      expect(result.pageSize).toBe(10);
      expect(result.hasMore).toBe(true);
    });
  });

  describe('getBudgetStatus', () => {
    it('fetches budget status for an entity', async () => {
      const mockResponse = {
        budget_amount_usd: 1000,
        current_spend_usd: 500,
        remaining_budget_usd: 500,
        budget_utilization_percent: 50,
        period_start: '2024-01-01T00:00:00Z',
        period_end: '2024-01-31T23:59:59Z',
        period_type: 'monthly',
        enforcement_mode: 'soft',
        budget_exceeded: false,
        warnings: [],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getBudgetStatus('org-1', EntityType.ORGANIZATION, 'org-1');

      expect(apiClient.get).toHaveBeenCalledWith(
        '/admin/organizations/org-1/budgets/org/org-1/status'
      );
      expect(result.budgetAmountUsd).toBe(1000);
      expect(result.currentSpendUsd).toBe(500);
      expect(result.budgetUtilizationPercent).toBe(50);
      expect(result.budgetExceeded).toBe(false);
    });

    it('returns exceeded status correctly', async () => {
      const mockResponse = {
        budget_amount_usd: 1000,
        current_spend_usd: 1200,
        remaining_budget_usd: -200,
        budget_utilization_percent: 120,
        period_start: '2024-01-01T00:00:00Z',
        period_end: '2024-01-31T23:59:59Z',
        period_type: 'monthly',
        enforcement_mode: 'hard',
        budget_exceeded: true,
        warnings: ['Budget exceeded by $200'],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getBudgetStatus('org-1', EntityType.DEPARTMENT, 'dept-1');

      expect(result.budgetExceeded).toBe(true);
      expect(result.warnings).toContain('Budget exceeded by $200');
      expect(result.enforcementMode).toBe(EnforcementMode.HARD);
    });
  });

  describe('createBudget', () => {
    it('creates a new budget', async () => {
      const mockResponse = {
        id: 'budget-new',
        entity_type: 'department',
        entity_id: 'dept-1',
        period_type: 'monthly',
        budget_amount_usd: 500,
        enforcement_mode: 'soft',
        org_id: 'org-1',
        updated_at: '2024-01-01T00:00:00Z',
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await createBudget('org-1', {
        entity_type: EntityType.DEPARTMENT,
        entity_id: 'dept-1',
        period_type: PeriodType.MONTHLY,
        budget_amount_usd: 500,
      });

      expect(apiClient.post).toHaveBeenCalledWith('/admin/organizations/org-1/budgets', {
        entity_type: EntityType.DEPARTMENT,
        entity_id: 'dept-1',
        period_type: PeriodType.MONTHLY,
        budget_amount_usd: 500,
      });
      // Note: createBudget now generates ID from entity_type-entity_id-period_type
      expect(result.id).toBe('department-dept-1-monthly');
      expect(result.budgetAmountUsd).toBe(500);
    });

    it('creates a budget with hard enforcement', async () => {
      const mockResponse = {
        id: 'budget-new',
        entity_type: 'team',
        entity_id: 'team-1',
        period_type: 'weekly',
        budget_amount_usd: 100,
        enforcement_mode: 'hard',
        org_id: 'org-1',
        updated_at: '2024-01-01T00:00:00Z',
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await createBudget('org-1', {
        entity_type: EntityType.TEAM,
        entity_id: 'team-1',
        period_type: PeriodType.WEEKLY,
        budget_amount_usd: 100,
        enforcement_mode: EnforcementMode.HARD,
      });

      expect(result.enforcementMode).toBe(EnforcementMode.HARD);
    });
  });

  describe('updateBudget', () => {
    it('updates an existing budget', async () => {
      const mockResponse = {
        id: 'budget-1',
        entity_type: 'org',
        entity_id: 'org-1',
        period_type: 'monthly',
        budget_amount_usd: 2000,
        enforcement_mode: 'hard',
        org_id: 'org-1',
        updated_at: '2024-01-02T00:00:00Z',
      };

      vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

      // updateBudget signature changed to (orgId, entityType, entityId, data)
      const result = await updateBudget('org-1', 'org', 'org-1', {
        budget_amount_usd: 2000,
        enforcement_mode: EnforcementMode.HARD,
      });

      expect(apiClient.put).toHaveBeenCalledWith('/admin/organizations/org-1/budget/org/org-1', {
        budget_amount_usd: 2000,
        enforcement_mode: EnforcementMode.HARD,
      });
      expect(result.budgetAmountUsd).toBe(2000);
    });
  });

  describe('deleteBudget', () => {
    it('deletes a budget by entity type, entity id, and period type', async () => {
      vi.mocked(apiClient.delete).mockResolvedValue({});

      await deleteBudget('org-1', 'user', 'user-1', 'monthly');

      expect(apiClient.delete).toHaveBeenCalledWith('/admin/organizations/org-1/budget/user/user-1/monthly');
    });
  });

  describe('getUsage', () => {
    it('fetches usage data for an organization', async () => {
      const mockResponse = {
        items: [
          {
            id: 'usage-1',
            entity_type: 'org',
            entity_id: 'org-1',
            period_start: '2024-01-01T00:00:00Z',
            period_type: 'monthly',
            total_cost_usd: 500,
            total_tokens: 100000,
            request_count: 1000,
            org_id: 'org-1',
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getUsage('org-1');

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/usage')
      );
      expect(result.items).toHaveLength(1);
      expect(result.items[0].totalCostUsd).toBe(500);
      expect(result.items[0].totalTokens).toBe(100000);
    });

    it('fetches usage with date range filter', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getUsage('org-1', {
        startDate: '2024-01-01',
        endDate: '2024-01-31',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/usage')
      );
    });

    it('fetches usage filtered by entity', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getUsage('org-1', {
        entityType: EntityType.DEPARTMENT,
        entityId: 'dept-1',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/usage')
      );
    });
  });

  describe('getUsageTimeSeries', () => {
    // Note: getUsageTimeSeries now expects a wrapped response with { data, period, org_id }
    // instead of a flat array, to match the backend response format
    it('fetches time series data with default granularity', async () => {
      const mockResponse = {
        data: [
          {
            date: '2024-01-01',
            input_tokens: 5000,
            output_tokens: 5000,
            cost_usd: 50,
            request_count: 100,
          },
          {
            date: '2024-01-02',
            input_tokens: 8000,
            output_tokens: 7000,
            cost_usd: 75,
            request_count: 150,
          },
        ],
        period: 'daily',
        org_id: 'org-1',
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getUsageTimeSeries('org-1', {
        startDate: '2024-01-01',
        endDate: '2024-01-07',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/usage/timeseries')
      );
      expect(result).toHaveLength(2);
      expect(result[0].requestCount).toBe(100);
      expect(result[0].tokenCount).toBe(10000); // input_tokens + output_tokens
      expect(result[0].costUsd).toBe(50);
      expect(result[0].errorCount).toBe(0); // Backend doesn't track errors
    });

    it('fetches time series data with hourly granularity', async () => {
      const mockResponse = {
        data: [
          {
            date: '2024-01-01',
            input_tokens: 500,
            output_tokens: 500,
            cost_usd: 5,
            request_count: 10,
          },
        ],
        period: 'daily',
        org_id: 'org-1',
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getUsageTimeSeries('org-1', {
        startDate: '2024-01-01',
        endDate: '2024-01-01',
        granularity: 'hour',
      });

      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/admin/organizations/org-1/usage/timeseries')
      );
    });

    it('fetches time series data for specific entity', async () => {
      const mockResponse = {
        data: [],
        period: 'weekly',
        org_id: 'org-1',
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getUsageTimeSeries('org-1', {
        entityType: EntityType.TEAM,
        entityId: 'team-1',
        startDate: '2024-01-01',
        endDate: '2024-01-31',
        granularity: 'week',
      });

      expect(apiClient.get).toHaveBeenCalled();
    });
  });

  describe('Error Handling', () => {
    it('propagates API errors', async () => {
      const error = { error: 'Not Found', message: 'Budget not found' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getBudgetStatus('org-1', EntityType.ORGANIZATION, 'invalid')).rejects.toEqual(
        error
      );
    });

    it('handles creation errors', async () => {
      const error = { error: 'Validation Error', message: 'Budget amount must be positive' };
      vi.mocked(apiClient.post).mockRejectedValue(error);

      await expect(
        createBudget('org-1', {
          entity_type: EntityType.ORGANIZATION,
          entity_id: 'org-1',
          period_type: PeriodType.MONTHLY,
          budget_amount_usd: -100,
        })
      ).rejects.toEqual(error);
    });
  });
});
