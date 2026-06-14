/**
 * API client for Agent Activity (invocation history).
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Issue #1461: Phase 6 — Chain view endpoint for lineage.
 * Mirrors the pattern in services/logs.ts.
 */

import { apiClient, buildQueryString } from './api';
import type {
  InvocationChainResponse,
  InvocationListResponse,
  InvocationQueryParams,
} from '@/types/activity';

/**
 * Fetch the current user's invocations.
 * Server scopes by token — no user_id param needed.
 */
export async function getMyInvocations(
  params?: InvocationQueryParams,
): Promise<InvocationListResponse> {
  const query = buildQueryString({
    status: params?.status,
    channel: params?.channel,
    persona: params?.persona,
    start_date: params?.start_date,
    end_date: params?.end_date,
    limit: params?.limit || 20,
    last_key: params?.last_key,
  });

  const response = await apiClient.get<InvocationListResponse>(
    `/me/agent-invocations${query}`,
  );

  return {
    items: Array.isArray(response?.items) ? response.items : [],
    last_key: response?.last_key ?? null,
  };
}

/**
 * Fetch all invocations (admin view).
 * Requires admin role — server enforces authz.
 */
export async function getAllInvocations(
  params?: InvocationQueryParams,
): Promise<InvocationListResponse> {
  const query = buildQueryString({
    status: params?.status,
    channel: params?.channel,
    persona: params?.persona,
    start_date: params?.start_date,
    end_date: params?.end_date,
    limit: params?.limit || 20,
    last_key: params?.last_key,
  });

  const response = await apiClient.get<InvocationListResponse>(
    `/admin/agent-invocations${query}`,
  );

  return {
    items: Array.isArray(response?.items) ? response.items : [],
    last_key: response?.last_key ?? null,
  };
}

/**
 * Fetch the chain view for a correlation_id (user's own invocations).
 */
export async function getMyInvocationChain(
  correlationId: string,
): Promise<InvocationChainResponse> {
  const response = await apiClient.get<InvocationChainResponse>(
    `/me/agent-invocations/chain/${encodeURIComponent(correlationId)}`,
  );
  return response;
}

/**
 * Fetch the chain view for a correlation_id (admin view).
 */
export async function getAdminInvocationChain(
  correlationId: string,
  tenantId?: string,
): Promise<InvocationChainResponse> {
  const query = tenantId ? buildQueryString({ tenant_id: tenantId }) : '';
  const response = await apiClient.get<InvocationChainResponse>(
    `/admin/agent-invocations/chain/${encodeURIComponent(correlationId)}${query}`,
  );
  return response;
}
