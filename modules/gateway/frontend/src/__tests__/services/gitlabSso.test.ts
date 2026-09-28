import { afterEach, describe, expect, it, vi } from 'vitest';
import { getAccessToken } from '@/services/auth';
import { handleGitlabSsoClick, startGitlabSso } from '@/services/gitlabSso';

vi.mock('@/services/auth', () => ({ getAccessToken: vi.fn(() => 'synthetic-gateway-token') }));
const nativeFetch = globalThis.fetch;
afterEach(() => { vi.unstubAllGlobals(); vi.mocked(getAccessToken).mockReturnValue('synthetic-gateway-token'); });
function navigation() {
  const location = { href: '' };
  vi.stubGlobal('window', { location });
  return location;
}

describe('GitLab configured handoff', () => {
  it.each([
    'https://gitlab.example.test/users/auth/jwt/callback?jwt=synthetic-sso-token',
    'https://adp.example.test/gitlab/users/auth/jwt/callback?jwt=synthetic-sso-token',
    'http://gitlab.dev.adp.internal/users/auth/jwt/callback?jwt=synthetic-sso-token',
  ])('navigates directly to the server-authorized callback %s', async (redirect_url) => {
    const location = navigation();
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ redirect_url })));
    vi.stubGlobal('fetch', fetcher);
    expect(startGitlabSso()).toBe(true);
    await vi.waitFor(() => expect(location.href).toBe(redirect_url));
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/api/auth/gitlab-sso', {
      headers: { Authorization: 'Bearer synthetic-gateway-token', Accept: 'application/json' }, redirect: 'error',
    });
  });
  it.each([
    null, {}, { redirect_url: 123 }, { redirect_url: '//attacker.test/users/auth/jwt/callback?jwt=x' },
    { redirect_url: 'javascript:alert(1)' }, { redirect_url: 'data:text/html,hello' },
    { redirect_url: 'https://user:password@gitlab.test/users/auth/jwt/callback?jwt=x' },
    { redirect_url: 'https://gitlab.test/users/auth/jwt/callback?jwt=x#fragment' },
    { redirect_url: 'https://gitlab.test/unexpected?jwt=x' },
    { redirect_url: 'https://gitlab.test/users/auth/jwt/callback' },
  ])('rejects malformed handoff %j', async (body) => {
    const location = navigation();
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)));
    vi.stubGlobal('fetch', fetcher);
    startGitlabSso();
    await vi.waitFor(() => expect(location.href).toBe('/gitlab/'));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it.each([401, 404, 503])('falls back on HTTP %s without retry', async (status) => {
    const location = navigation();
    const fetcher = vi.fn().mockResolvedValue(new Response('{}', { status }));
    vi.stubGlobal('fetch', fetcher);
    startGitlabSso();
    await vi.waitFor(() => expect(location.href).toBe('/gitlab/'));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('does not retry a browser opaque redirect with automatic following', async () => {
    const location = navigation();
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ type: 'opaqueredirect', ok: false, redirected: false })
      .mockResolvedValueOnce({ redirected: true, url: 'https://attacker.test/' });
    vi.stubGlobal('fetch', fetcher);
    startGitlabSso();
    await vi.waitFor(() => expect(location.href).toBe('/gitlab/'));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('keeps un-tokened anchors local without a request', () => {
    vi.mocked(getAccessToken).mockReturnValue(null);
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    const preventDefault = vi.fn();
    handleGitlabSsoClick({ preventDefault });
    expect(preventDefault).not.toHaveBeenCalled();
    expect(fetcher).not.toHaveBeenCalled();
  });
});

// Native HTTP transport with ephemeral loopback fixtures and synthetic tokens.
for (const origin of ['same-origin', 'cross-origin']) {
  for (const status of [200, 301, 302, 303, 307, 308]) {
    it(`native ${origin} HTTP ${status} only permits a direct JSON handoff`, async () => {
      const { createServer } = await import('node:http');
      const listen = async (server: ReturnType<typeof createServer>) => {
        await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
        const address = server.address();
        if (!address || typeof address === 'string') throw new Error('missing fixture port');
        return address.port;
      };
      let destinationRequests = 0;
      const receivedTokens: string[] = [];
      const target = createServer((_request, response) => { destinationRequests++; response.end('{}'); });
      const targetPort = await listen(target);
      const source = createServer((request, response) => {
        if (request.url === '/destination') { destinationRequests++; response.end('{}'); return; }
        receivedTokens.push(request.headers.authorization ?? '');
        if (status === 200) {
          response.writeHead(200, { 'content-type': 'application/json' });
          response.end(JSON.stringify({ redirect_url: `http://127.0.0.1:${targetPort}/users/auth/jwt/callback?jwt=synthetic-sso-token` }));
          return;
        }
        response.writeHead(status, { location: origin === 'same-origin' ? '/destination' : `http://127.0.0.1:${targetPort}/destination` });
        response.end();
      });
      const sourcePort = await listen(source);
      const location = navigation();
      vi.stubGlobal('fetch', (_input: RequestInfo | URL, init?: RequestInit) => nativeFetch(`http://127.0.0.1:${sourcePort}/api/auth/gitlab-sso`, init));
      try {
        startGitlabSso();
        await vi.waitFor(() => expect(location.href).toBe(status === 200 ? `http://127.0.0.1:${targetPort}/users/auth/jwt/callback?jwt=synthetic-sso-token` : '/gitlab/'));
        expect(destinationRequests).toBe(0);
        expect(receivedTokens).toEqual(['Bearer synthetic-gateway-token']);
      } finally {
        source.closeAllConnections(); target.closeAllConnections();
        await Promise.all([source, target].map(server => new Promise<void>(resolve => server.close(() => resolve()))));
      }
    });
  }
}
