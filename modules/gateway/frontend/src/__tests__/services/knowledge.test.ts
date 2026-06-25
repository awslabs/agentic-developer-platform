/**
 * Unit tests for the knowledge service layer.
 *
 * Issue #1794 (Story E of E10 #1736).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { listAssets, getAssetDetail, createAsset, deleteAsset, reindexAsset, getAccessibleRepos } from '@/services/knowledge';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
  buildQueryString: vi.fn((params: Record<string, unknown>) => {
    const parts: string[] = [];
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') {
        parts.push(`${key}=${value}`);
      }
    }
    return parts.length ? `?${parts.join('&')}` : '';
  }),
}));

import { apiClient } from '@/services/api';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;
const mockPost = apiClient.post as ReturnType<typeof vi.fn>;
const mockDelete = apiClient.delete as ReturnType<typeof vi.fn>;

const mockApiAsset = {
  id: 'asset-001',
  asset_type: 'repo',
  source_ref: 'https://github.com/acme/my-service',
  display_name: 'acme/my-service',
  tags: {},
  metadata: {},
  tenant_id: 'org-001',
  owner_sub: null,
  project_id: null,
  status: 'indexed',
  last_error: null,
  retry_count: 0,
  registered_by: 'user-001',
  created_at: '2026-06-20T10:00:00Z',
  updated_at: '2026-06-20T11:00:00Z',
};

describe('knowledge service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('listAssets', () => {
    it('fetches assets and transforms snake_case to camelCase', async () => {
      mockGet.mockResolvedValue({
        items: [mockApiAsset],
        total: 1,
        page: 1,
        page_size: 20,
        has_more: false,
        quota: { repos: { used: 1, limit: 20 }, urls: null, docs: null },
      });

      const result = await listAssets({ page: 1, pageSize: 20 });

      expect(mockGet).toHaveBeenCalledTimes(1);
      expect(result.items).toHaveLength(1);
      expect(result.items[0].id).toBe('asset-001');
      expect(result.items[0].assetType).toBe('repo');
      expect(result.items[0].sourceRef).toBe('https://github.com/acme/my-service');
      expect(result.items[0].displayName).toBe('acme/my-service');
      expect(result.items[0].tenantId).toBe('org-001');
      expect(result.items[0].status).toBe('indexed');
      expect(result.total).toBe(1);
      expect(result.hasMore).toBe(false);
      expect(result.quota?.repos?.used).toBe(1);
    });

    it('handles empty response', async () => {
      mockGet.mockResolvedValue({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        has_more: false,
        quota: null,
      });

      const result = await listAssets();

      expect(result.items).toHaveLength(0);
      expect(result.total).toBe(0);
      expect(result.quota).toBeNull();
    });

    it('passes scope filter', async () => {
      mockGet.mockResolvedValue({
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        has_more: false,
        quota: null,
      });

      await listAssets({ scope: 'personal' });

      const callArg = mockGet.mock.calls[0][0] as string;
      expect(callArg).toContain('scope=personal');
    });
  });

  describe('getAssetDetail', () => {
    it('fetches and transforms a single asset', async () => {
      mockGet.mockResolvedValue(mockApiAsset);

      const result = await getAssetDetail('asset-001');

      expect(mockGet).toHaveBeenCalledTimes(1);
      expect(mockGet.mock.calls[0][0]).toBe('/api/agent-context/assets/asset-001');
      expect(result.id).toBe('asset-001');
      expect(result.assetType).toBe('repo');
    });
  });

  describe('createAsset', () => {
    it('posts the request and returns transformed asset', async () => {
      mockPost.mockResolvedValue(mockApiAsset);

      const result = await createAsset({
        asset_type: 'repo',
        source_ref: 'https://github.com/acme/my-service',
        display_name: 'acme/my-service',
        scope: 'tenant',
      });

      expect(mockPost).toHaveBeenCalledTimes(1);
      expect(mockPost.mock.calls[0][0]).toBe('/api/agent-context/assets');
      expect(mockPost.mock.calls[0][1]).toEqual({
        asset_type: 'repo',
        source_ref: 'https://github.com/acme/my-service',
        display_name: 'acme/my-service',
        scope: 'tenant',
      });
      expect(result.id).toBe('asset-001');
    });
  });

  describe('deleteAsset', () => {
    it('calls DELETE on the correct endpoint', async () => {
      mockDelete.mockResolvedValue({});

      await deleteAsset('asset-001');

      expect(mockDelete).toHaveBeenCalledWith('/api/agent-context/assets/asset-001');
    });
  });

  describe('reindexAsset', () => {
    it('posts to reindex and returns the updated asset', async () => {
      mockPost.mockResolvedValue({ ...mockApiAsset, status: 'registered' });

      const result = await reindexAsset('asset-001');

      expect(mockPost).toHaveBeenCalledWith('/api/agent-context/assets/asset-001/reindex');
      expect(result.status).toBe('registered');
    });
  });

  describe('getAccessibleRepos', () => {
    it('fetches repos and transforms full_name to fullName', async () => {
      mockGet.mockResolvedValue({
        repos: [
          { full_name: 'acme/service-a', private: true, url: 'https://github.com/acme/service-a' },
          { full_name: 'acme/docs', private: false, url: 'https://github.com/acme/docs' },
        ],
        total: 2,
        page: 1,
        has_more: false,
      });

      const result = await getAccessibleRepos({ search: 'acme', page: 1 });

      expect(result.repos).toHaveLength(2);
      expect(result.repos[0].fullName).toBe('acme/service-a');
      expect(result.repos[0].private).toBe(true);
      expect(result.repos[1].fullName).toBe('acme/docs');
      expect(result.total).toBe(2);
      expect(result.hasMore).toBe(false);
    });

    it('handles empty repos', async () => {
      mockGet.mockResolvedValue({
        repos: [],
        total: 0,
        page: 1,
        has_more: false,
      });

      const result = await getAccessibleRepos();

      expect(result.repos).toHaveLength(0);
      expect(result.total).toBe(0);
    });
  });
});
