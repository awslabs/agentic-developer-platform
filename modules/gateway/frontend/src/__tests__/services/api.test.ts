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
      // apiClient reads the token via getAccessToken(), which uses the
      // 'cognito_access_token' sessionStorage key (see services/auth.ts).
      sessionStorage.setItem('cognito_access_token', 'test-token');
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

  describe('DELETE requests', () => {
    it('serializes a JSON body with its content type', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve(JSON.stringify({ status: 'not-configured' })),
      });

      const body = { expected_revision: 9 };
      await client.delete('/persona-models/developer', body);

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:3000/api/persona-models/developer',
        expect.objectContaining({
          method: 'DELETE',
          body: JSON.stringify(body),
          headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
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

      // `status` accompanies the server's parsed body (#5730). Callers need it to
      // tell "you are not permitted" from "this is not deployed here" from "retry
      // shortly" — outcomes that can arrive with an identical body, and which ask
      // different things of the user. Without it every failure renders the same
      // generic banner.
      await expect(client.get('/test')).rejects.toEqual({
        error: 'Bad Request',
        message: 'Invalid input',
        status: 400,
      });
    });

    it('reports the real status, not one inferred from the body', async () => {
      // Guards against the status being hardcoded or copied from the payload: a
      // body claiming one thing and a response saying another must resolve to the
      // response, which is the authority.
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        json: () => Promise.resolve({ error: 'upstream', message: 'unavailable', status: 200 }),
      });

      await expect(client.get('/test')).rejects.toMatchObject({ status: 503 });
    });

    it('attaches the status even when the error body is not JSON', async () => {
      // A proxy or load balancer failure often returns HTML. The synthesised
      // fallback body must carry the status too, or those failures are exactly
      // the ones a caller cannot classify.
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 502,
        statusText: 'Bad Gateway',
        json: () => Promise.reject(new Error('not json')),
      });

      await expect(client.get('/test')).rejects.toMatchObject({ status: 502 });
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

const nativeFetchForRedirects = globalThis.fetch;
describe('API redirect boundaries', () => {
  let client: ApiClient;
  let mockFetch: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    mockFetch = vi.fn();
    globalThis.fetch = mockFetch;
    client = new ApiClient('/api');
  });
  afterEach(() => { globalThis.fetch = nativeFetchForRedirects; });
for (const destination of ['same-origin', 'cross-origin']) {
  for (const status of [200, 302, 307, 308]) {
    it(`refuses HTTP ${status} ${destination} redirects before forwarding API authorization`, async () => {
      const { createServer } = await import('node:http');
      const listen = async (server: ReturnType<typeof createServer>) => {
        await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
        const address = server.address();
        if (!address || typeof address === 'string') throw new Error('missing fixture port');
        return address.port;
      };
      let forwarded = 0;
      const target = createServer((_request, response) => {
        forwarded += 1;
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end('{}');
      });
      const targetPort = await listen(target);
      const source = createServer((request, response) => {
        if (request.url === '/forwarded') forwarded += 1;
        if (status === 200 || request.url === '/forwarded') {
          response.writeHead(200, { 'content-type': 'application/json' });
          response.end('{}');
          return;
        }
        response.writeHead(status, { location: destination === 'same-origin'
          ? '/forwarded' : `http://127.0.0.1:${targetPort}/forwarded` });
        response.end();
      });
      const sourcePort = await listen(source);
      mockFetch.mockImplementation((_url, init) => nativeFetchForRedirects(`http://127.0.0.1:${sourcePort}/fixture`, init));
      sessionStorage.setItem('cognito_access_token', 'test-token');
      try {
        if (status === 200) await expect(client.get('/fixture')).resolves.toEqual({});
        else await expect(client.get('/fixture')).rejects.toThrow();
        expect(mockFetch.mock.calls[0][1]).toMatchObject({
          redirect: 'error', headers: { Authorization: 'Bearer test-token' },
        });
        expect(forwarded).toBe(0);
      } finally {
        source.closeAllConnections(); target.closeAllConnections();
        await Promise.all([source, target].map(server => new Promise<void>(resolve => server.close(() => resolve()))));
        sessionStorage.removeItem('cognito_access_token');
      }
    });
  }
}
});
