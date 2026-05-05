/**
 * Connections API service tests.
 *
 * Issue #477: Test coverage for the Connections page (parent #465).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  startGitHubInstall,
  listConnections,
  deleteGitHubConnection,
  type InstallStartResponse,
  type ConnectionsListResponse,
  type DeleteConnectionResponse,
} from '@/services/connections';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  buildQueryString: vi.fn(),
}));

describe('Connections Service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('startGitHubInstall', () => {
    it('calls POST /api/admin/connections/github/install-start with empty body', async () => {
      const mockResponse: InstallStartResponse = {
        install_url: 'https://github.com/apps/adp-agent/installations/new?state=abc123',
        state_token: 'abc123',
        expires_at: '2026-05-05T12:00:00Z',
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await startGitHubInstall();

      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/admin/connections/github/install-start',
        {},
      );
      expect(result.install_url).toContain('github.com');
      expect(result.state_token).toBe('abc123');
      expect(result.expires_at).toBe('2026-05-05T12:00:00Z');
    });

    it('returns parsed response with install_url containing state param', async () => {
      const mockResponse: InstallStartResponse = {
        install_url: 'https://github.com/apps/adp-agent/installations/new?state=xyz789',
        state_token: 'xyz789',
        expires_at: '2026-05-05T13:00:00Z',
      };

      vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

      const result = await startGitHubInstall();

      expect(result.install_url).toContain('state=');
    });
  });

  describe('listConnections', () => {
    it('calls GET /api/admin/connections and returns connections array', async () => {
      const mockResponse: ConnectionsListResponse = {
        connections: [
          {
            provider: 'github',
            installation_id: 12345,
            account_login: 'my-org',
            account_type: 'Organization',
            repository_selection: 'selected',
            repository_count: 5,
            installed_at: '2026-05-01T10:00:00Z',
            configure_url: 'https://github.com/organizations/my-org/settings/installations/12345',
          },
        ],
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await listConnections();

      expect(apiClient.get).toHaveBeenCalledWith('/api/admin/connections');
      expect(result.connections).toHaveLength(1);
      expect(result.connections[0].installation_id).toBe(12345);
      expect(result.connections[0].account_login).toBe('my-org');
    });

    it('returns empty array when no connections exist', async () => {
      const mockResponse: ConnectionsListResponse = { connections: [] };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await listConnections();

      expect(result.connections).toHaveLength(0);
    });
  });

  describe('deleteGitHubConnection', () => {
    it('calls DELETE /api/admin/connections/github/{installationId}', async () => {
      const mockResponse: DeleteConnectionResponse = {
        deleted: true,
        installation_id: 12345,
      };

      vi.mocked(apiClient.delete).mockResolvedValue(mockResponse);

      const result = await deleteGitHubConnection(12345);

      expect(apiClient.delete).toHaveBeenCalledWith(
        '/api/admin/connections/github/12345',
      );
      expect(result.deleted).toBe(true);
      expect(result.installation_id).toBe(12345);
    });

    it('uses the correct installation ID in the URL path', async () => {
      vi.mocked(apiClient.delete).mockResolvedValue({
        deleted: true,
        installation_id: 99999,
      });

      await deleteGitHubConnection(99999);

      expect(apiClient.delete).toHaveBeenCalledWith(
        '/api/admin/connections/github/99999',
      );
    });
  });

  describe('Error Handling', () => {
    it('propagates API errors from startGitHubInstall', async () => {
      const error = { error: 'Unauthorized', message: 'Invalid token' };
      vi.mocked(apiClient.post).mockRejectedValue(error);

      await expect(startGitHubInstall()).rejects.toEqual(error);
    });

    it('propagates API errors from listConnections', async () => {
      const error = { error: 'Internal Server Error', message: 'Database unavailable' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(listConnections()).rejects.toEqual(error);
    });

    it('propagates API errors from deleteGitHubConnection', async () => {
      const error = { error: 'Forbidden', message: 'Admin role required' };
      vi.mocked(apiClient.delete).mockRejectedValue(error);

      await expect(deleteGitHubConnection(123)).rejects.toEqual(error);
    });
  });

  describe('Type Safety', () => {
    it('response shapes match expected interfaces at runtime', async () => {
      const installResponse: InstallStartResponse = {
        install_url: 'https://github.com/apps/test/installations/new?state=s1',
        state_token: 's1',
        expires_at: '2026-12-31T23:59:59Z',
      };
      vi.mocked(apiClient.post).mockResolvedValue(installResponse);

      const result = await startGitHubInstall();

      // Verify all required fields exist and have correct types
      expect(typeof result.install_url).toBe('string');
      expect(typeof result.state_token).toBe('string');
      expect(typeof result.expires_at).toBe('string');
    });
  });
});
