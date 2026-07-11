/**
 * API client for Agent Activity (invocation history).
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Issue #1461: Phase 6 — Chain view endpoint for lineage.
 * Mirrors the pattern in services/logs.ts.
 */

import { apiClient, buildQueryString } from './api';
import type {
  ChainListResponse,
  InvocationChainResponse,
  InvocationItem,
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
    include_non_triggering: params?.include_non_triggering,
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
 * Fetch the current user's invocations grouped by chain.
 * Issue #1662: Chain-grouped board view — one row per chain.
 */
export async function getMyChains(
  params?: InvocationQueryParams,
): Promise<ChainListResponse> {
  const query = buildQueryString({
    view: 'chains',
    status: params?.status,
    channel: params?.channel,
    persona: params?.persona,
    start_date: params?.start_date,
    end_date: params?.end_date,
    limit: params?.limit || 20,
    last_key: params?.last_key,
    include_non_triggering: params?.include_non_triggering,
  });

  const response = await apiClient.get<ChainListResponse>(
    `/me/agent-invocations${query}`,
  );

  return {
    chains: Array.isArray(response?.chains) ? response.chains : [],
    count: response?.count ?? 0,
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
    include_non_triggering: params?.include_non_triggering,
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
 *
 * Issue #3708: Accepts include_non_triggering to control whether no_op and
 * webhook_received items appear in the chain (default: excluded).
 */
export async function getMyInvocationChain(
  correlationId: string,
  includeNonTriggering?: boolean,
): Promise<InvocationChainResponse> {
  const query = buildQueryString({
    include_non_triggering: includeNonTriggering,
  });
  const response = await apiClient.get<InvocationChainResponse>(
    `/me/agent-invocations/chain/${encodeURIComponent(correlationId)}${query}`,
  );
  return response;
}

/**
 * Fetch the chain view for a correlation_id (admin view).
 *
 * Issue #3708: Accepts include_non_triggering to control whether no_op and
 * webhook_received items appear in the chain (default: excluded).
 */
export async function getAdminInvocationChain(
  correlationId: string,
  tenantId?: string,
  includeNonTriggering?: boolean,
): Promise<InvocationChainResponse> {
  const query = buildQueryString({
    tenant_id: tenantId,
    include_non_triggering: includeNonTriggering,
  });
  const response = await apiClient.get<InvocationChainResponse>(
    `/admin/agent-invocations/chain/${encodeURIComponent(correlationId)}${query}`,
  );
  return response;
}

/**
 * Fetch a single invocation detail (user's own).
 * Issue #1653: Dedicated detail endpoint with full cost enrichment.
 */
export async function getMyInvocationDetail(
  invocationId: string,
): Promise<InvocationItem> {
  return apiClient.get<InvocationItem>(
    `/me/agent-invocations/${encodeURIComponent(invocationId)}`,
  );
}

/**
 * Fetch a single invocation detail (admin view).
 * Issue #1653: Admin variant with tenant scoping.
 */
export async function getAdminInvocationDetail(
  invocationId: string,
  tenantId?: string,
): Promise<InvocationItem> {
  const query = tenantId ? buildQueryString({ tenant_id: tenantId }) : '';
  return apiClient.get<InvocationItem>(
    `/admin/agent-invocations/${encodeURIComponent(invocationId)}${query}`,
  );
}

/**
 * Fetch the transcript markdown for a user's own invocation.
 * Issue #3069: Returns raw markdown text (text/markdown content type).
 * Uses raw fetch because apiClient.get() expects JSON responses.
 */
export async function getMyTranscript(invocationId: string): Promise<string> {
  const { getAccessToken } = await import('./auth');
  const baseUrl = import.meta.env.VITE_API_URL || '/api';
  const token = getAccessToken();
  const response = await fetch(
    `${baseUrl}/me/agent-invocations/${encodeURIComponent(invocationId)}/transcript`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    },
  );
  if (!response.ok) {
    throw new Error(response.status === 404 ? 'Transcript not available' : `Failed to load transcript (${response.status})`);
  }
  return response.text();
}

/**
 * Fetch the transcript markdown for an invocation (admin view).
 * Issue #3069: Admin variant with tenant scoping.
 */
export async function getAdminTranscript(
  invocationId: string,
  tenantId?: string,
): Promise<string> {
  const { getAccessToken } = await import('./auth');
  const baseUrl = import.meta.env.VITE_API_URL || '/api';
  const token = getAccessToken();
  const query = tenantId ? buildQueryString({ tenant_id: tenantId }) : '';
  // nosemgrep: javascript-ssrf-rule-node_ssrf — browser-side fetch of own API base; SSRF is not a client-side vulnerability
  const response = await fetch(
    `${baseUrl}/admin/agent-invocations/${encodeURIComponent(invocationId)}/transcript${query}`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    },
  );
  if (!response.ok) {
    throw new Error(response.status === 404 ? 'Transcript not available' : `Failed to load transcript (${response.status})`);
  }
  return response.text();
}
