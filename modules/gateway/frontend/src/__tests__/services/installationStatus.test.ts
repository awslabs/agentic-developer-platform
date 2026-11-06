import { afterEach, expect, it, vi } from 'vitest';
import { getInstallationStatus } from '@/services/installationStatus';

vi.mock('@/services/auth', () => ({ getAccessToken: () => 'test-token' }));

afterEach(() => vi.unstubAllGlobals());

it('reads installation status through the authenticated, encoded gateway path', async () => {
  const status = { installation_id: '101', status: 'unavailable' };
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(status)));
  vi.stubGlobal('fetch', fetch);
  const controller = new AbortController();
  await expect(getInstallationStatus('101', controller.signal)).resolves.toEqual(status);
  expect(fetch).toHaveBeenCalledWith('/api/assistant/installations/101/status', expect.objectContaining({
    method: 'GET', headers: expect.objectContaining({ Authorization: 'Bearer test-token' }), signal: controller.signal, redirect: 'error',
  }));
});

it('encodes a reference without accepting query-selected identity', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}'));
  vi.stubGlobal('fetch', fetch);
  await getInstallationStatus('101?tenant_id=other');
  expect(fetch.mock.calls[0][0]).toBe('/api/assistant/installations/101%3Ftenant_id%3Dother/status');
});
