/**
 * Knowledge-assets service layer.
 *
 * Issue #1794 (Story E of E10 #1736): Asset CRUD + repo picker calls.
 * Backend: /api/agent-context/assets (PR #1891), /api/agent-context/github (PR #1907).
 */

import { apiClient, buildQueryString } from './api';
import { getAccessToken } from './auth';
import type {
  KnowledgeAsset,
  AssetListResponse,
  AssetCreateRequest,
  AssetStatusResponse,
  AssetIndexStage,
  AccessibleRepo,
  AccessibleReposResponse,
  BulkPreviewResponse,
  BulkCommitRequest,
  BulkCommitResponse,
} from '@/types';

// ---------------------------------------------------------------------------
// Asset CRUD
// ---------------------------------------------------------------------------

/** List/filter knowledge assets for the current user's scope. */
export async function listAssets(params?: {
  scope?: string;
  assetType?: string;
  status?: string;
  page?: number;
  pageSize?: number;
}): Promise<AssetListResponse> {
  const query = buildQueryString({
    scope: params?.scope,
    asset_type: params?.assetType,
    status: params?.status,
    page: params?.page || 1,
    page_size: params?.pageSize || 20,
  });

  const response = await apiClient.get<{
    items: Array<{
      id: string;
      asset_type: string;
      source_ref: string;
      display_name: string | null;
      tags: Record<string, unknown>;
      metadata: Record<string, unknown>;
      tenant_id: string | null;
      owner_sub: string | null;
      project_id: string | null;
      status: string;
      last_error: string | null;
      retry_count: number;
      registered_by: string | null;
      created_at: string;
      updated_at: string | null;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
    quota: {
      repos: { used: number; limit: number } | null;
      urls: { used: number; limit: number } | null;
      docs: { used: number; limit: number } | null;
    } | null;
  }>(`/api/agent-context/assets${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformAsset),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 20,
    hasMore: response?.has_more ?? false,
    quota: response?.quota
      ? {
          repos: response.quota.repos,
          urls: response.quota.urls,
          docs: response.quota.docs,
        }
      : null,
  };
}

/** Get asset detail by ID. */
export async function getAssetDetail(assetId: string): Promise<KnowledgeAsset> {
  const response = await apiClient.get<{
    id: string;
    asset_type: string;
    source_ref: string;
    display_name: string | null;
    tags: Record<string, unknown>;
    metadata: Record<string, unknown>;
    tenant_id: string | null;
    owner_sub: string | null;
    project_id: string | null;
    status: string;
    last_error: string | null;
    retry_count: number;
    registered_by: string | null;
    created_at: string;
    updated_at: string | null;
  }>(`/api/agent-context/assets/${assetId}`);
  return transformAsset(response);
}

/** Register a new asset. */
export async function createAsset(body: AssetCreateRequest): Promise<KnowledgeAsset> {
  const response = await apiClient.post<{
    id: string;
    asset_type: string;
    source_ref: string;
    display_name: string | null;
    tags: Record<string, unknown>;
    metadata: Record<string, unknown>;
    tenant_id: string | null;
    owner_sub: string | null;
    project_id: string | null;
    status: string;
    last_error: string | null;
    retry_count: number;
    registered_by: string | null;
    created_at: string;
    updated_at: string | null;
  }>('/api/agent-context/assets', body);
  return transformAsset(response);
}

/** Soft-delete an asset. */
export async function deleteAsset(assetId: string): Promise<void> {
  await apiClient.delete(`/api/agent-context/assets/${assetId}`);
}

/** Re-queue an asset for indexing. */
export async function reindexAsset(assetId: string): Promise<KnowledgeAsset> {
  const response = await apiClient.post<{
    id: string;
    asset_type: string;
    source_ref: string;
    display_name: string | null;
    tags: Record<string, unknown>;
    metadata: Record<string, unknown>;
    tenant_id: string | null;
    owner_sub: string | null;
    project_id: string | null;
    status: string;
    last_error: string | null;
    retry_count: number;
    registered_by: string | null;
    created_at: string;
    updated_at: string | null;
  }>(`/api/agent-context/assets/${assetId}/reindex`);
  return transformAsset(response);
}

/** Get per-tool indexing status for an asset (joins index_run_stages). */
export async function getAssetStatus(assetId: string): Promise<AssetStatusResponse> {
  const response = await apiClient.get<{
    asset_id: string;
    source_ref: string;
    repo_found: boolean;
    run_id: string | null;
    run_status: string | null;
    run_started_at: string | null;
    stages: Array<{
      stage: string;
      status: string;
      artifact_ref: string | null;
      error: string | null;
      started_at: string | null;
      completed_at: string | null;
      metrics: Record<string, any> | null;
      worker_pod: string | null;
    }>;
  }>(`/api/agent-context/assets/${assetId}/status`);

  return {
    assetId: response.asset_id,
    sourceRef: response.source_ref,
    repoFound: response.repo_found,
    runId: response.run_id ?? null,
    runStatus: response.run_status ?? null,
    runStartedAt: response.run_started_at ?? null,
    stages: (response.stages ?? []).map(transformStage),
  };
}

// ---------------------------------------------------------------------------
// Repo picker
// ---------------------------------------------------------------------------

/** List repos accessible to the caller's tenant GitHub App. */
export async function getAccessibleRepos(params?: {
  search?: string;
  page?: number;
  pageSize?: number;
}): Promise<AccessibleReposResponse> {
  const query = buildQueryString({
    search: params?.search,
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });

  const response = await apiClient.get<{
    repos: Array<{ full_name: string; private: boolean; url: string }>;
    total: number;
    page: number;
    has_more: boolean;
  }>(`/api/agent-context/github/accessible-repos${query}`);

  const repos = Array.isArray(response?.repos) ? response.repos : [];
  return {
    repos: repos.map(transformRepo),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    hasMore: response?.has_more ?? false,
  };
}

// ---------------------------------------------------------------------------
// Bulk upload (Issue #1795 — Story F)
// ---------------------------------------------------------------------------

/**
 * Upload a file for bulk preview (no DB writes).
 * Uses multipart/form-data — bypasses apiClient's JSON serialization.
 */
export async function bulkPreview(
  file: File,
  scope: 'personal' | 'tenant' = 'tenant',
): Promise<BulkPreviewResponse> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('scope', scope);

  const token = getAccessToken();
  const headers: Record<string, string> = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch('/api/agent-context/assets/bulk', {
    method: 'POST',
    headers,
    body: formData,
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: response.statusText }));
    throw errorData;
  }

  return response.json();
}

/** Commit the validated items from a bulk preview. */
export async function bulkCommit(body: BulkCommitRequest): Promise<BulkCommitResponse> {
  const raw = await apiClient.post<{
    created: number;
    skipped_duplicates: number;
    assets: Array<{
      id: string;
      asset_type: string;
      source_ref: string;
      display_name: string | null;
      tags: Record<string, unknown>;
      metadata: Record<string, unknown>;
      tenant_id: string | null;
      owner_sub: string | null;
      project_id: string | null;
      status: string;
      last_error: string | null;
      retry_count: number;
      registered_by: string | null;
      created_at: string;
      updated_at: string | null;
    }>;
  }>('/api/agent-context/assets/bulk/commit', body);

  return {
    created: raw.created,
    skipped_duplicates: raw.skipped_duplicates,
    assets: (raw.assets || []).map(transformAsset),
  };
}

// ---------------------------------------------------------------------------
// Transform helpers (snake_case API → camelCase frontend)
// ---------------------------------------------------------------------------

function transformAsset(raw: {
  id: string;
  asset_type: string;
  source_ref: string;
  display_name: string | null;
  tags: Record<string, unknown>;
  metadata: Record<string, unknown>;
  tenant_id: string | null;
  owner_sub: string | null;
  project_id: string | null;
  status: string;
  last_error: string | null;
  retry_count: number;
  registered_by: string | null;
  created_at: string;
  updated_at: string | null;
}): KnowledgeAsset {
  return {
    id: raw.id,
    assetType: raw.asset_type,
    sourceRef: raw.source_ref,
    displayName: raw.display_name,
    tags: raw.tags ?? {},
    metadata: raw.metadata ?? {},
    tenantId: raw.tenant_id,
    ownerSub: raw.owner_sub,
    projectId: raw.project_id,
    status: raw.status as KnowledgeAsset['status'],
    lastError: raw.last_error,
    retryCount: raw.retry_count ?? 0,
    registeredBy: raw.registered_by,
    createdAt: raw.created_at,
    updatedAt: raw.updated_at,
  };
}

function transformRepo(raw: {
  full_name: string;
  private: boolean;
  url: string;
}): AccessibleRepo {
  return {
    fullName: raw.full_name,
    private: raw.private,
    url: raw.url,
  };
}

function transformStage(raw: {
  stage: string;
  status: string;
  artifact_ref: string | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  metrics: Record<string, any> | null;
  worker_pod: string | null;
}): AssetIndexStage {
  return {
    stage: raw.stage,
    status: raw.status,
    artifactRef: raw.artifact_ref,
    error: raw.error,
    startedAt: raw.started_at,
    completedAt: raw.completed_at,
    metrics: raw.metrics,
    workerPod: raw.worker_pod,
  };
}
