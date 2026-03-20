import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import { getPlatformDashboard, getOrgDashboard, getSystemMetrics } from '@/services/dashboard';
import { PeriodType, EnforcementMode } from '@/types';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

describe('Dashboard Service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('getPlatformDashboard', () => {
    it('fetches platform dashboard data', async () => {
      const mockResponse = {
        total_organizations: 10,
        total_requests_24h: 5000,
        total_tokens_24h: 1000000,
        total_cost_24h: 500.5,
        active_users_24h: 50,
        error_rate_24h: 0.02,
        pool_status: {
          total_accounts: 5,
          healthy_accounts: 4,
          unhealthy_accounts: 1,
          accounts: [
            {
              id: 'acc-1',
              account_id: '123456789012',
              role_arn: 'arn:aws:iam::123456789012:role/BedrockRole',
              region: 'us-east-1',
              is_healthy: true,
              last_health_check: '2024-01-01T12:00:00Z',
              created_at: '2024-01-01T00:00:00Z',
            },
          ],
        },
        top_organizations: [
          {
            id: 'org-1',
            name: 'Top Org',
            request_count: 1000,
            token_count: 200000,
            cost_usd: 100,
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPlatformDashboard();

      expect(apiClient.get).toHaveBeenCalledWith('/admin/dashboard/platform');
      expect(result.totalOrganizations).toBe(10);
      expect(result.totalRequests24h).toBe(5000);
      expect(result.totalTokens24h).toBe(1000000);
      expect(result.totalCost24h).toBe(500.5);
      expect(result.activeUsers24h).toBe(50);
      expect(result.errorRate24h).toBe(0.02);
    });

    it('transforms pool status data correctly', async () => {
      const mockResponse = {
        total_organizations: 1,
        total_requests_24h: 100,
        total_tokens_24h: 10000,
        total_cost_24h: 5,
        active_users_24h: 5,
        error_rate_24h: 0,
        pool_status: {
          total_accounts: 3,
          healthy_accounts: 2,
          unhealthy_accounts: 1,
          accounts: [
            {
              id: 'acc-1',
              account_id: '111111111111',
              role_arn: 'arn:aws:iam::111111111111:role/Role1',
              region: 'us-west-2',
              is_healthy: true,
              last_health_check: '2024-01-01T10:00:00Z',
              created_at: '2024-01-01T00:00:00Z',
            },
            {
              id: 'acc-2',
              account_id: '222222222222',
              role_arn: 'arn:aws:iam::222222222222:role/Role2',
              region: 'eu-west-1',
              is_healthy: false,
              last_health_check: null,
              created_at: '2024-01-01T00:00:00Z',
            },
          ],
        },
        top_organizations: [],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPlatformDashboard();

      expect(result.poolStatus.totalAccounts).toBe(3);
      expect(result.poolStatus.healthyAccounts).toBe(2);
      expect(result.poolStatus.unhealthyAccounts).toBe(1);
      expect(result.poolStatus.accounts).toHaveLength(2);
      expect(result.poolStatus.accounts[0].accountId).toBe('111111111111');
      expect(result.poolStatus.accounts[0].roleArn).toBe(
        'arn:aws:iam::111111111111:role/Role1'
      );
      expect(result.poolStatus.accounts[0].isHealthy).toBe(true);
      expect(result.poolStatus.accounts[1].lastHealthCheck).toBeNull();
    });

    it('transforms top organizations correctly', async () => {
      const mockResponse = {
        total_organizations: 5,
        total_requests_24h: 10000,
        total_tokens_24h: 2000000,
        total_cost_24h: 1000,
        active_users_24h: 100,
        error_rate_24h: 0.01,
        pool_status: {
          total_accounts: 1,
          healthy_accounts: 1,
          unhealthy_accounts: 0,
          accounts: [],
        },
        top_organizations: [
          {
            id: 'org-1',
            name: 'Organization Alpha',
            request_count: 5000,
            token_count: 1000000,
            cost_usd: 500,
          },
          {
            id: 'org-2',
            name: 'Organization Beta',
            request_count: 3000,
            token_count: 600000,
            cost_usd: 300,
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPlatformDashboard();

      expect(result.topOrganizations).toHaveLength(2);
      expect(result.topOrganizations[0].id).toBe('org-1');
      expect(result.topOrganizations[0].name).toBe('Organization Alpha');
      expect(result.topOrganizations[0].requestCount).toBe(5000);
      expect(result.topOrganizations[0].tokenCount).toBe(1000000);
      expect(result.topOrganizations[0].costUsd).toBe(500);
    });
  });

  describe('getOrgDashboard', () => {
    it('fetches organization dashboard data', async () => {
      const mockResponse = {
        org_id: 'org-1',
        org_name: 'Test Organization',
        total_requests_24h: 1000,
        total_tokens_24h: 200000,
        total_cost_24h: 100,
        active_users_24h: 10,
        error_rate_24h: 0.01,
        budget_status: {
          budget_amount_usd: 500,
          current_spend_usd: 250,
          remaining_budget_usd: 250,
          budget_utilization_percent: 50,
          period_start: '2024-01-01T00:00:00Z',
          period_end: '2024-01-31T23:59:59Z',
          period_type: 'monthly',
          enforcement_mode: 'soft',
          budget_exceeded: false,
          warnings: [],
        },
        top_departments: [
          {
            id: 'dept-1',
            name: 'Engineering',
            request_count: 500,
            token_count: 100000,
            cost_usd: 50,
          },
        ],
        top_models: [
          {
            name: 'claude-3-sonnet',
            request_count: 800,
            token_count: 160000,
            cost_usd: 80,
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getOrgDashboard('org-1');

      expect(apiClient.get).toHaveBeenCalledWith('/admin/dashboard/org/org-1');
      expect(result.orgId).toBe('org-1');
      expect(result.orgName).toBe('Test Organization');
      expect(result.totalRequests24h).toBe(1000);
      expect(result.totalTokens24h).toBe(200000);
      expect(result.totalCost24h).toBe(100);
      expect(result.activeUsers24h).toBe(10);
      expect(result.errorRate24h).toBe(0.01);
    });

    it('transforms budget status correctly', async () => {
      const mockResponse = {
        org_id: 'org-1',
        org_name: 'Test Organization',
        total_requests_24h: 0,
        total_tokens_24h: 0,
        total_cost_24h: 0,
        active_users_24h: 0,
        error_rate_24h: 0,
        budget_status: {
          budget_amount_usd: 1000,
          current_spend_usd: 900,
          remaining_budget_usd: 100,
          budget_utilization_percent: 90,
          period_start: '2024-01-01T00:00:00Z',
          period_end: '2024-01-31T23:59:59Z',
          period_type: 'monthly',
          enforcement_mode: 'hard',
          budget_exceeded: false,
          warnings: ['Approaching budget limit'],
        },
        top_departments: [],
        top_models: [],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getOrgDashboard('org-1');

      expect(result.budgetStatus!.budgetAmountUsd).toBe(1000);
      expect(result.budgetStatus!.currentSpendUsd).toBe(900);
      expect(result.budgetStatus!.remainingBudgetUsd).toBe(100);
      expect(result.budgetStatus!.budgetUtilizationPercent).toBe(90);
      expect(result.budgetStatus!.periodType).toBe(PeriodType.MONTHLY);
      expect(result.budgetStatus!.enforcementMode).toBe(EnforcementMode.HARD);
      expect(result.budgetStatus!.warnings).toContain('Approaching budget limit');
    });

    it('transforms top departments correctly', async () => {
      const mockResponse = {
        org_id: 'org-1',
        org_name: 'Test',
        total_requests_24h: 0,
        total_tokens_24h: 0,
        total_cost_24h: 0,
        active_users_24h: 0,
        error_rate_24h: 0,
        budget_status: {
          budget_amount_usd: 0,
          current_spend_usd: 0,
          remaining_budget_usd: 0,
          budget_utilization_percent: 0,
          period_start: '2024-01-01T00:00:00Z',
          period_end: '2024-01-31T23:59:59Z',
          period_type: 'monthly',
          enforcement_mode: 'soft',
          budget_exceeded: false,
          warnings: [],
        },
        top_departments: [
          {
            id: 'dept-1',
            name: 'Engineering',
            request_count: 1000,
            token_count: 200000,
            cost_usd: 100,
          },
          {
            id: 'dept-2',
            name: 'Sales',
            request_count: 500,
            token_count: 100000,
            cost_usd: 50,
          },
        ],
        top_models: [],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getOrgDashboard('org-1');

      expect(result.topDepartments).toHaveLength(2);
      expect(result.topDepartments[0].id).toBe('dept-1');
      expect(result.topDepartments[0].name).toBe('Engineering');
      expect(result.topDepartments[0].requestCount).toBe(1000);
    });

    it('transforms top models correctly', async () => {
      const mockResponse = {
        org_id: 'org-1',
        org_name: 'Test',
        total_requests_24h: 0,
        total_tokens_24h: 0,
        total_cost_24h: 0,
        active_users_24h: 0,
        error_rate_24h: 0,
        budget_status: {
          budget_amount_usd: 0,
          current_spend_usd: 0,
          remaining_budget_usd: 0,
          budget_utilization_percent: 0,
          period_start: '2024-01-01T00:00:00Z',
          period_end: '2024-01-31T23:59:59Z',
          period_type: 'monthly',
          enforcement_mode: 'soft',
          budget_exceeded: false,
          warnings: [],
        },
        top_departments: [],
        top_models: [
          {
            name: 'claude-3-opus',
            request_count: 100,
            token_count: 50000,
            cost_usd: 75,
          },
          {
            name: 'claude-3-sonnet',
            request_count: 500,
            token_count: 200000,
            cost_usd: 50,
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getOrgDashboard('org-1');

      expect(result.topModels).toHaveLength(2);
      expect(result.topModels[0].name).toBe('claude-3-opus');
      expect(result.topModels[0].requestCount).toBe(100);
      expect(result.topModels[0].tokenCount).toBe(50000);
      expect(result.topModels[0].costUsd).toBe(75);
    });
  });

  describe('getSystemMetrics', () => {
    it('fetches system metrics', async () => {
      const mockResponse = {
        api_calls_per_minute: 1000,
        average_latency_ms: 150,
        error_rate: 0.005,
        active_connections: 50,
        cpu_utilization: 45.5,
        memory_utilization: 60.2,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getSystemMetrics();

      expect(apiClient.get).toHaveBeenCalledWith('/admin/metrics/system');
      expect(result.apiCallsPerMinute).toBe(1000);
      expect(result.averageLatencyMs).toBe(150);
      expect(result.errorRate).toBe(0.005);
      expect(result.activeConnections).toBe(50);
      expect(result.cpuUtilization).toBe(45.5);
      expect(result.memoryUtilization).toBe(60.2);
    });

    it('handles zero metrics', async () => {
      const mockResponse = {
        api_calls_per_minute: 0,
        average_latency_ms: 0,
        error_rate: 0,
        active_connections: 0,
        cpu_utilization: 0,
        memory_utilization: 0,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getSystemMetrics();

      expect(result.apiCallsPerMinute).toBe(0);
      expect(result.errorRate).toBe(0);
    });
  });

  describe('Error Handling', () => {
    it('propagates errors from platform dashboard', async () => {
      const error = { error: 'Unauthorized', message: 'Not authenticated' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getPlatformDashboard()).rejects.toEqual(error);
    });

    it('propagates errors from org dashboard', async () => {
      const error = { error: 'Forbidden', message: 'Access denied' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getOrgDashboard('org-1')).rejects.toEqual(error);
    });

    it('propagates errors from system metrics', async () => {
      const error = { error: 'Internal Server Error', message: 'Database unavailable' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getSystemMetrics()).rejects.toEqual(error);
    });
  });
});
