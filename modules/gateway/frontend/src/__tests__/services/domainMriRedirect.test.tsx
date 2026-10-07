import { render, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import DomainMRI from '@/pages/DomainMRI';

const mount = vi.hoisted(() => vi.fn(() => vi.fn()));
vi.mock('@/pages/domain-mri/mount', () => ({ mountDomainMRI: mount }));
vi.mock('@/services/auth', () => ({ getAccessToken: () => 'test-token', clearTokens: vi.fn() }));
vi.mock('@/config/runtime', () => ({ deploymentSetting: () => '/api' }));
afterEach(() => { vi.unstubAllGlobals(); mount.mockClear(); });

it('keeps mounted demo requests authenticated without following redirects', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ entries: [] }), {
    headers: { 'content-type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fetch);
  const view = render(<DomainMRI />);
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  expect(fetch.mock.calls[0][1]).toMatchObject({
    redirect: 'error', headers: { Authorization: 'Bearer test-token' },
  });
  const fetcher = mount.mock.calls[0][1] as typeof globalThis.fetch;
  await fetcher('/v1/tasks/tsk_12345678-1234-4123-8123-123456789abc');
  expect(fetch.mock.calls[1][0]).toMatch(/^\/api\/v1\/tasks\//);
  expect(fetch.mock.calls[1][1]).toMatchObject({
    redirect: 'error', headers: { Authorization: 'Bearer test-token' },
  });
  view.unmount();
});
