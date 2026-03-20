import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import { getLogs, getLogEntry, exportLogs, downloadLogs } from '@/services/logs';

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

describe('Logs Service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.setItem('auth_token', 'test-token');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  describe('getLogs', () => {
    it('fetches logs with default parameters', async () => {
      const mockResponse = {
        items: [
          {
            id: 'log-1',
            timestamp: '2024-01-01T12:00:00Z',
            org_id: 'org-1',
            user_id: 'user-1',
            method: 'POST',
            path: '/v1/chat/completions',
            status_code: 200,
            response_time_ms: 150,
            request_body_size: 100,
            response_body_size: 500,
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getLogs();

      expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/logs'));
      expect(result.items).toHaveLength(1);
      expect(result.items[0].id).toBe('log-1');
      expect(result.items[0].orgId).toBe('org-1');
      expect(result.items[0].userId).toBe('user-1');
      expect(result.items[0].method).toBe('POST');
      expect(result.items[0].path).toBe('/v1/chat/completions');
      expect(result.items[0].statusCode).toBe(200);
      expect(result.items[0].responseTimeMs).toBe(150);
    });

    it('fetches logs with time range filter', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getLogs({
        start_time: '2024-01-01T00:00:00Z',
        end_time: '2024-01-31T23:59:59Z',
      });

      expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/logs'));
    });

    it('fetches logs filtered by organization', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getLogs({ org_id: 'org-1' });

      expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/logs'));
    });

    it('fetches logs filtered by user', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getLogs({ user_id: 'user-1' });

      expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/logs'));
    });

    it('fetches logs filtered by status code', async () => {
      const mockResponse = {
        items: [
          {
            id: 'log-1',
            timestamp: '2024-01-01T12:00:00Z',
            org_id: 'org-1',
            user_id: 'user-1',
            method: 'POST',
            path: '/v1/chat/completions',
            status_code: 500,
            response_time_ms: 50,
            request_body_size: 100,
            response_body_size: null,
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getLogs({ status_code: 500 });

      expect(result.items[0].statusCode).toBe(500);
    });

    it('fetches logs filtered by path pattern', async () => {
      const mockResponse = {
        items: [],
        total: 0,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      await getLogs({ path_pattern: '/v1/chat/*' });

      expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/logs'));
    });

    it('fetches logs filtered by minimum response time', async () => {
      const mockResponse = {
        items: [
          {
            id: 'log-1',
            timestamp: '2024-01-01T12:00:00Z',
            org_id: 'org-1',
            user_id: 'user-1',
            method: 'POST',
            path: '/v1/chat/completions',
            status_code: 200,
            response_time_ms: 5000,
            request_body_size: 100,
            response_body_size: 500,
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getLogs({ min_response_time_ms: 1000 });

      expect(result.items[0].responseTimeMs).toBeGreaterThanOrEqual(1000);
    });

    it('fetches logs with pagination', async () => {
      const mockResponse = {
        items: [],
        total: 100,
        page: 3,
        page_size: 20,
        has_more: true,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getLogs({ page: 3, page_size: 20 });

      expect(result.page).toBe(3);
      expect(result.pageSize).toBe(20);
      expect(result.total).toBe(100);
      expect(result.hasMore).toBe(true);
    });

    it('handles null body sizes', async () => {
      const mockResponse = {
        items: [
          {
            id: 'log-1',
            timestamp: '2024-01-01T12:00:00Z',
            org_id: 'org-1',
            user_id: 'user-1',
            method: 'GET',
            path: '/health',
            status_code: 200,
            response_time_ms: 10,
            request_body_size: null,
            response_body_size: null,
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getLogs();

      expect(result.items[0].requestBodySize).toBeNull();
      expect(result.items[0].responseBodySize).toBeNull();
    });
  });

  describe('getLogEntry', () => {
    it('fetches a single log entry by ID', async () => {
      const mockResponse = {
        id: 'log-1',
        timestamp: '2024-01-01T12:00:00Z',
        org_id: 'org-1',
        user_id: 'user-1',
        method: 'POST',
        path: '/v1/chat/completions',
        status_code: 200,
        response_time_ms: 150,
        request_body_size: 100,
        response_body_size: 500,
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getLogEntry('log-1');

      expect(apiClient.get).toHaveBeenCalledWith('/admin/logs/log-1');
      expect(result.id).toBe('log-1');
      expect(result.timestamp).toBe('2024-01-01T12:00:00Z');
      expect(result.orgId).toBe('org-1');
    });
  });

  describe('exportLogs', () => {
    it('exports logs as CSV blob', async () => {
      const mockBlob = new Blob(['id,timestamp,org_id\nlog-1,2024-01-01,org-1'], {
        type: 'text/csv',
      });
      const mockResponse = {
        ok: true,
        blob: vi.fn().mockResolvedValue(mockBlob),
      };

      globalThis.fetch = vi.fn().mockResolvedValue(mockResponse);

      const result = await exportLogs();

      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/admin/logs/export'),
        expect.objectContaining({
          headers: {
            Authorization: 'Bearer test-token',
          },
        })
      );
      expect(result).toBeInstanceOf(Blob);
    });

    it('exports logs with filters', async () => {
      const mockBlob = new Blob(['csv data'], { type: 'text/csv' });
      const mockResponse = {
        ok: true,
        blob: vi.fn().mockResolvedValue(mockBlob),
      };

      globalThis.fetch = vi.fn().mockResolvedValue(mockResponse);

      await exportLogs({
        org_id: 'org-1',
        start_time: '2024-01-01T00:00:00Z',
        end_time: '2024-01-31T23:59:59Z',
      });

      expect(fetch).toHaveBeenCalled();
    });

    it('throws error on failed export', async () => {
      const mockResponse = {
        ok: false,
        status: 500,
      };

      globalThis.fetch = vi.fn().mockResolvedValue(mockResponse);

      await expect(exportLogs()).rejects.toThrow('Failed to export logs');
    });
  });

  describe('downloadLogs', () => {
    it('downloads blob as file', () => {
      const mockBlob = new Blob(['csv data'], { type: 'text/csv' });

      // Mock URL.createObjectURL and revokeObjectURL
      const mockUrl = 'blob:http://localhost/mock-url';
      URL.createObjectURL = vi.fn().mockReturnValue(mockUrl);
      URL.revokeObjectURL = vi.fn();

      // Mock document.createElement and document.body methods
      const mockAnchor = {
        href: '',
        download: '',
        click: vi.fn(),
      };
      document.createElement = vi.fn().mockReturnValue(mockAnchor);
      document.body.appendChild = vi.fn();
      document.body.removeChild = vi.fn();

      downloadLogs(mockBlob, 'logs.csv');

      expect(URL.createObjectURL).toHaveBeenCalledWith(mockBlob);
      expect(mockAnchor.href).toBe(mockUrl);
      expect(mockAnchor.download).toBe('logs.csv');
      expect(mockAnchor.click).toHaveBeenCalled();
      expect(document.body.appendChild).toHaveBeenCalled();
      expect(document.body.removeChild).toHaveBeenCalled();
      expect(URL.revokeObjectURL).toHaveBeenCalledWith(mockUrl);
    });

    it('uses default filename when not provided', () => {
      const mockBlob = new Blob(['csv data'], { type: 'text/csv' });

      URL.createObjectURL = vi.fn().mockReturnValue('blob:http://localhost/mock-url');
      URL.revokeObjectURL = vi.fn();

      const mockAnchor = {
        href: '',
        download: '',
        click: vi.fn(),
      };
      document.createElement = vi.fn().mockReturnValue(mockAnchor);
      document.body.appendChild = vi.fn();
      document.body.removeChild = vi.fn();

      downloadLogs(mockBlob);

      expect(mockAnchor.download).toBe('logs.csv');
    });
  });

  describe('Error Handling', () => {
    it('propagates API errors from getLogs', async () => {
      const error = { error: 'Unauthorized', message: 'Not authenticated' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getLogs()).rejects.toEqual(error);
    });

    it('propagates API errors from getLogEntry', async () => {
      const error = { error: 'Not Found', message: 'Log entry not found' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getLogEntry('invalid-id')).rejects.toEqual(error);
    });
  });
});
