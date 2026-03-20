import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient, buildQueryString } from '@/services/api';

describe('ApiClient', () => {
  let client: ApiClient;
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockFetch = vi.fn();
    globalThis.fetch = mockFetch;
    client = new ApiClient('http://localhost:3000/api');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('GET requests', () => {
    it('makes GET request with correct URL', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve(JSON.stringify({ data: 'test' })),
      });

      await client.get('/test');

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:3000/api/test',
        expect.objectContaining({
          method: 'GET',
        })
      );
    });

    it('includes authorization header when token is present', async () => {
      sessionStorage.setItem('auth_token', 'test-token');
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve(JSON.stringify({})),
      });

      await client.get('/test');

      expect(mockFetch).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: 'Bearer test-token',
          }),
        })
      );
    });
  });

  describe('POST requests', () => {
    it('makes POST request with body', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve(JSON.stringify({ id: 1 })),
      });

      const body = { name: 'test' };
      await client.post('/test', body);

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:3000/api/test',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify(body),
        })
      );
    });
  });

  describe('Error handling', () => {
    it('throws error for non-ok responses', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 400,
        statusText: 'Bad Request',
        json: () => Promise.resolve({ error: 'Bad Request', message: 'Invalid input' }),
      });

      await expect(client.get('/test')).rejects.toEqual({
        error: 'Bad Request',
        message: 'Invalid input',
      });
    });

    it('handles 401 by clearing token', async () => {
      sessionStorage.setItem('auth_token', 'test-token');
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        json: () => Promise.resolve({ error: 'Unauthorized', message: 'Invalid token' }),
      });

      // Mock window.location
      const originalLocation = window.location;
      delete (window as unknown as { location?: Location }).location;
      window.location = { ...originalLocation, href: '' } as Location;

      await expect(client.get('/test')).rejects.toBeDefined();
      expect(sessionStorage.getItem('auth_token')).toBeNull();

      window.location = originalLocation;
    });
  });

  describe('Response handling', () => {
    it('handles empty response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve(''),
      });

      const result = await client.get('/test');
      expect(result).toEqual({});
    });

    it('parses JSON response', async () => {
      const data = { id: 1, name: 'test' };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(data)),
      });

      const result = await client.get('/test');
      expect(result).toEqual(data);
    });
  });
});

describe('buildQueryString', () => {
  it('builds empty string for empty params', () => {
    expect(buildQueryString({})).toBe('');
  });

  it('builds query string from params', () => {
    const result = buildQueryString({ page: 1, limit: 10 });
    expect(result).toBe('?page=1&limit=10');
  });

  it('ignores null and undefined values', () => {
    const result = buildQueryString({ page: 1, filter: null, search: undefined });
    expect(result).toBe('?page=1');
  });

  it('ignores empty strings', () => {
    const result = buildQueryString({ page: 1, search: '' });
    expect(result).toBe('?page=1');
  });

  it('handles string values', () => {
    const result = buildQueryString({ name: 'test', status: 'active' });
    expect(result).toBe('?name=test&status=active');
  });
});
