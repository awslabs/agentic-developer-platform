import { afterEach, expect, it, vi } from 'vitest';
import { readChatMode, selectChatMode, endChatSession } from '@/services/chatMode';

vi.mock('@/services/auth', () => ({ getAccessToken: () => 'test-token' }));

afterEach(() => vi.unstubAllGlobals());

it('reads and selects mode through the authenticated gateway session route', async () => {
  const state = { mode: 'persistent', health: 'idle', sequence: 2 };
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(state)));
  vi.stubGlobal('fetch', fetch);
  await expect(readChatMode('sess-1')).resolves.toEqual(state);
  await expect(selectChatMode('sess-1', 'persistent')).resolves.toEqual(state);
  await expect(endChatSession('sess-1')).resolves.toEqual(state);
  expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/chat/sessions/sess-1/mode', '/api/chat/sessions/sess-1/mode', '/api/chat/sessions/sess-1/end']);
  expect(fetch.mock.calls[0][1]).toMatchObject({ method: 'GET', headers: { Authorization: 'Bearer test-token' } });
  expect(fetch.mock.calls[1][1]).toMatchObject({ method: 'PUT', body: JSON.stringify({ mode: 'persistent' }) });
  expect(fetch.mock.calls[2][1]).toMatchObject({ method: 'POST', headers: { Authorization: 'Bearer test-token' } });
});
