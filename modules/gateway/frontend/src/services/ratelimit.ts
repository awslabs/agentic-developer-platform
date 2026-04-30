/**
 * Rate Limit Service
 *
 * Issue #185: Budget & Rate Limit Management UI for Org Admins
 * API service for managing rate limit configurations.
 */

import { apiClient, buildQueryString } from './api';
import type { PaginatedResponse } from '@/types/api';
import type { EntityType } from '@/types';

// Rate limit list item interface
export interface RateLimitListItem {
  entityType: EntityType;
  entityId: string;
  rpm: number | null;
  tpm: number | null;
  concurrentRequests: number | null;
  updatedAt: string;
}

// Rate limit create request
export interface RateLimitCreateRequest {
  entity_type: string;
  entity_id: string;
  rpm?: number | null;
  tpm?: number | null;
  concurrent_requests?: number | null;
}

// Rate limit update request
export interface RateLimitUpdateRequest {
  rpm?: number | null;
  tpm?: number | null;
  concurrent_requests?: number | null;
}

// Get all rate limits for an organization
export async function getRatelimits(
  orgId: string,
  params?: {
    entityType?: EntityType;
    page?: number;
    limit?: number;
  }
): Promise<PaginatedResponse<RateLimitListItem>> {
  const query = buildQueryString({
    entity_type: params?.entityType,
    page: params?.page || 1,
    limit: params?.limit || 20,
  });

  const response = await apiClient.get<{
    items: Array<{
      entity_type: string;
      entity_id: string;
      rpm: number | null;
      tpm: number | null;
      concurrent_requests: number | null;
      updated_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/ratelimits${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformRateLimitListItem),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 20,
    hasMore: response?.has_more ?? false,
  };
}

// Get a single rate limit config
export async function getRatelimit(
  orgId: string,
  entityType: string,
  entityId: string
): Promise<RateLimitListItem | null> {
  const response = await apiClient.get<{
    org_id: string;
    entity_type: string;
    entity_id: string;
    rpm: number | null;
    tpm: number | null;
    concurrent_requests: number | null;
    updated_at: string;
  } | null>(`/admin/organizations/${orgId}/ratelimit/${entityType}/${entityId}`);

  if (!response) return null;

  return transformRateLimitListItem(response);
}

// Create a new rate limit config
export async function createRatelimit(
  orgId: string,
  data: RateLimitCreateRequest
): Promise<RateLimitListItem> {
  const response = await apiClient.post<{
    org_id: string;
    entity_type: string;
    entity_id: string;
    rpm: number | null;
    tpm: number | null;
    concurrent_requests: number | null;
    updated_at: string;
  }>(`/admin/organizations/${orgId}/ratelimits`, data);

  return transformRateLimitListItem(response);
}

// Update a rate limit config
export async function updateRatelimit(
  orgId: string,
  entityType: string,
  entityId: string,
  data: RateLimitUpdateRequest
): Promise<RateLimitListItem> {
  const response = await apiClient.put<{
    org_id: string;
    entity_type: string;
    entity_id: string;
    rpm: number | null;
    tpm: number | null;
    concurrent_requests: number | null;
    updated_at: string;
  }>(`/admin/organizations/${orgId}/ratelimit/${entityType}/${entityId}`, data);

  return transformRateLimitListItem(response);
}

// Delete a rate limit config
export async function deleteRatelimit(
  orgId: string,
  entityType: string,
  entityId: string
): Promise<void> {
  await apiClient.delete(`/admin/organizations/${orgId}/ratelimit/${entityType}/${entityId}`);
}

// Transform API response to frontend format
function transformRateLimitListItem(data: {
  entity_type: string;
  entity_id: string;
  rpm: number | null;
  tpm: number | null;
  concurrent_requests: number | null;
  updated_at: string;
}): RateLimitListItem {
  return {
    entityType: data.entity_type as EntityType,
    entityId: data.entity_id,
    rpm: data.rpm,
    tpm: data.tpm,
    concurrentRequests: data.concurrent_requests,
    updatedAt: data.updated_at,
  };
}
