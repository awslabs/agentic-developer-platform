import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getPoolStatus,
  addPoolAccount,
  removePoolAccount,
  triggerHealthCheck,
} from '@/services/pool';

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

describe('Pool Service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('getPoolStatus', () => {
    it('fetches pool status successfully', async () => {
      const mockResponse = {
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
          {
            id: 'acc-2',
            account_id: '210987654321',
            role_arn: 'arn:aws:iam::210987654321:role/BedrockRole',
            region: 'us-west-2',
            is_healthy: false,
            last_health_check: '2024-01-01T11:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPoolStatus();

      expect(apiClient.get).toHaveBeenCalledWith('/admin/pool/status');
      expect(result.totalAccounts).toBe(5);
      expect(result.healthyAccounts).toBe(4);
      expect(result.unhealthyAccounts).toBe(1);
      expect(result.accounts).toHaveLength(2);
    });

    it('transforms account data correctly', async () => {
      const mockResponse = {
        total_accounts: 1,
        healthy_accounts: 1,
        unhealthy_accounts: 0,
        accounts: [
          {
            id: 'acc-1',
            account_id: '123456789012',
            role_arn: 'arn:aws:iam::123456789012:role/BedrockRole',
            region: 'eu-west-1',
            is_healthy: true,
            last_health_check: '2024-01-01T12:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPoolStatus();

      expect(result.accounts[0].id).toBe('acc-1');
      expect(result.accounts[0].accountId).toBe('123456789012');
      expect(result.accounts[0].roleArn).toBe('arn:aws:iam::123456789012:role/BedrockRole');
      expect(result.accounts[0].region).toBe('eu-west-1');
      expect(result.accounts[0].isHealthy).toBe(true);
      expect(result.accounts[0].lastHealthCheck).toBe('2024-01-01T12:00:00Z');
      expect(result.accounts[0].createdAt).toBe('2024-01-01T00:00:00Z');
    });

    it('handles null lastHealthCheck', async () => {
      const mockResponse = {
        total_accounts: 1,
        healthy_accounts: 0,
        unhealthy_accounts: 1,
        accounts: [
          {
            id: 'acc-1',
            account_id: '123456789012',
            role_arn: 'arn:aws:iam::123456789012:role/BedrockRole',
            region: 'us-east-1',
            is_healthy: false,
            last_health_check: null,
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPoolStatus();

      expect(result.accounts[0].lastHealthCheck).toBeNull();
    });

    it('handles empty account list', async () => {
      const mockResponse = {
        total_accounts: 0,
        healthy_accounts: 0,
        unhealthy_accounts: 0,
        accounts: [],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getPoolStatus();

      expect(result.totalAccounts).toBe(0);
      expect(result.accounts).toHaveLength(0);
    });
  });

  describe('addPoolAccount', () => {
    it('adds a new pool account', async () => {
      const mockResponse = {
        id: 'acc-new',
        account_id: '111122223333',
        role_arn: 'arn:aws:iam::111122223333:role/NewRole',
        region: 'us-east-1',
        is_healthy: true,
        last_health_check: null,
        created_at: '2024-01-02T00:00:00Z',
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await addPoolAccount({
        account_id: '111122223333',
        role_arn: 'arn:aws:iam::111122223333:role/NewRole',
      });

      expect(apiClient.post).toHaveBeenCalledWith('/admin/pool/accounts', {
        account_id: '111122223333',
        role_arn: 'arn:aws:iam::111122223333:role/NewRole',
      });
      expect(result.id).toBe('acc-new');
      expect(result.accountId).toBe('111122223333');
      expect(result.roleArn).toBe('arn:aws:iam::111122223333:role/NewRole');
      expect(result.isHealthy).toBe(true);
    });

    it('adds a new pool account with region', async () => {
      const mockResponse = {
        id: 'acc-new',
        account_id: '111122223333',
        role_arn: 'arn:aws:iam::111122223333:role/NewRole',
        region: 'ap-southeast-1',
        is_healthy: true,
        last_health_check: null,
        created_at: '2024-01-02T00:00:00Z',
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await addPoolAccount({
        account_id: '111122223333',
        role_arn: 'arn:aws:iam::111122223333:role/NewRole',
        region: 'ap-southeast-1',
      });

      expect(apiClient.post).toHaveBeenCalledWith('/admin/pool/accounts', {
        account_id: '111122223333',
        role_arn: 'arn:aws:iam::111122223333:role/NewRole',
        region: 'ap-southeast-1',
      });
      expect(result.region).toBe('ap-southeast-1');
    });
  });

  describe('removePoolAccount', () => {
    it('removes a pool account', async () => {
      vi.mocked(apiClient.delete).mockResolvedValue({});

      await removePoolAccount('acc-1');

      expect(apiClient.delete).toHaveBeenCalledWith('/admin/pool/accounts/acc-1');
    });

    it('handles removal errors', async () => {
      const error = { error: 'Not Found', message: 'Account not found' };
      vi.mocked(apiClient.delete).mockRejectedValue(error);

      await expect(removePoolAccount('invalid-id')).rejects.toEqual(error);
    });
  });

  describe('triggerHealthCheck', () => {
    it('triggers health check and returns updated status', async () => {
      const mockResponse = {
        total_accounts: 3,
        healthy_accounts: 2,
        unhealthy_accounts: 1,
        accounts: [
          {
            id: 'acc-1',
            account_id: '123456789012',
            role_arn: 'arn:aws:iam::123456789012:role/BedrockRole',
            region: 'us-east-1',
            is_healthy: true,
            last_health_check: '2024-01-02T10:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
          {
            id: 'acc-2',
            account_id: '210987654321',
            role_arn: 'arn:aws:iam::210987654321:role/BedrockRole',
            region: 'us-west-2',
            is_healthy: true,
            last_health_check: '2024-01-02T10:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
          {
            id: 'acc-3',
            account_id: '333333333333',
            role_arn: 'arn:aws:iam::333333333333:role/BedrockRole',
            region: 'eu-west-1',
            is_healthy: false,
            last_health_check: '2024-01-02T10:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await triggerHealthCheck();

      expect(apiClient.post).toHaveBeenCalledWith('/admin/pool/health-check');
      expect(result.totalAccounts).toBe(3);
      expect(result.healthyAccounts).toBe(2);
      expect(result.unhealthyAccounts).toBe(1);
      expect(result.accounts).toHaveLength(3);
    });

    it('transforms accounts after health check', async () => {
      const mockResponse = {
        total_accounts: 1,
        healthy_accounts: 1,
        unhealthy_accounts: 0,
        accounts: [
          {
            id: 'acc-1',
            account_id: '123456789012',
            role_arn: 'arn:aws:iam::123456789012:role/BedrockRole',
            region: 'us-east-1',
            is_healthy: true,
            last_health_check: '2024-01-02T10:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await triggerHealthCheck();

      expect(result.accounts[0].accountId).toBe('123456789012');
      expect(result.accounts[0].isHealthy).toBe(true);
      expect(result.accounts[0].lastHealthCheck).toBe('2024-01-02T10:00:00Z');
    });

    it('handles all accounts becoming unhealthy', async () => {
      const mockResponse = {
        total_accounts: 2,
        healthy_accounts: 0,
        unhealthy_accounts: 2,
        accounts: [
          {
            id: 'acc-1',
            account_id: '123456789012',
            role_arn: 'arn:aws:iam::123456789012:role/BedrockRole',
            region: 'us-east-1',
            is_healthy: false,
            last_health_check: '2024-01-02T10:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
          {
            id: 'acc-2',
            account_id: '210987654321',
            role_arn: 'arn:aws:iam::210987654321:role/BedrockRole',
            region: 'us-west-2',
            is_healthy: false,
            last_health_check: '2024-01-02T10:00:00Z',
            created_at: '2024-01-01T00:00:00Z',
          },
        ],
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await triggerHealthCheck();

      expect(result.healthyAccounts).toBe(0);
      expect(result.unhealthyAccounts).toBe(2);
      expect(result.accounts.every((acc) => !acc.isHealthy)).toBe(true);
    });
  });

  describe('Error Handling', () => {
    it('propagates API errors from getPoolStatus', async () => {
      const error = { error: 'Unauthorized', message: 'Not authenticated' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getPoolStatus()).rejects.toEqual(error);
    });

    it('propagates API errors from addPoolAccount', async () => {
      const error = { error: 'Validation Error', message: 'Invalid role ARN' };
      vi.mocked(apiClient.post).mockRejectedValue(error);

      await expect(
        addPoolAccount({
          account_id: '123456789012',
          role_arn: 'invalid-arn',
        })
      ).rejects.toEqual(error);
    });

    it('propagates API errors from triggerHealthCheck', async () => {
      const error = { error: 'Internal Server Error', message: 'Health check failed' };
      vi.mocked(apiClient.post).mockRejectedValue(error);

      await expect(triggerHealthCheck()).rejects.toEqual(error);
    });

    it('handles network errors', async () => {
      vi.mocked(apiClient.get).mockRejectedValue(new Error('Network error'));

      await expect(getPoolStatus()).rejects.toThrow('Network error');
    });
  });
});
