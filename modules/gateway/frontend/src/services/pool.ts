import { apiClient } from './api';
import type { PoolStatus, PoolAccount, PoolAccountCreateRequest } from '@/types';

export async function getPoolStatus(): Promise<PoolStatus> {
  const response = await apiClient.get<{
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
  }>('/admin/pool/status');

  return {
    totalAccounts: response.total_accounts,
    healthyAccounts: response.healthy_accounts,
    unhealthyAccounts: response.unhealthy_accounts,
    accounts: response.accounts.map(transformPoolAccount),
  };
}

export async function addPoolAccount(data: PoolAccountCreateRequest): Promise<PoolAccount> {
  const response = await apiClient.post<{
    id: string;
    account_id: string;
    role_arn: string;
    region: string;
    is_healthy: boolean;
    last_health_check: string | null;
    created_at: string;
  }>('/admin/pool/accounts', data);
  return transformPoolAccount(response);
}

export async function removePoolAccount(accountId: string): Promise<void> {
  await apiClient.delete(`/admin/pool/accounts/${accountId}`);
}

export async function triggerHealthCheck(): Promise<PoolStatus> {
  const response = await apiClient.post<{
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
  }>('/admin/pool/health-check');

  return {
    totalAccounts: response.total_accounts,
    healthyAccounts: response.healthy_accounts,
    unhealthyAccounts: response.unhealthy_accounts,
    accounts: response.accounts.map(transformPoolAccount),
  };
}

function transformPoolAccount(data: {
  id: string;
  account_id: string;
  role_arn: string;
  region: string;
  is_healthy: boolean;
  last_health_check: string | null;
  created_at: string;
}): PoolAccount {
  return {
    id: data.id,
    accountId: data.account_id,
    roleArn: data.role_arn,
    region: data.region,
    isHealthy: data.is_healthy,
    lastHealthCheck: data.last_health_check,
    createdAt: data.created_at,
  };
}
