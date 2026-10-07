import { afterEach, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { readChatReplay } from '@/services/chatReplay';

afterEach(() => vi.restoreAllMocks());

it('requests an owner-authorized replay page with a bounded limit and encoded cursor', async () => {
  const page = { status: 'ok', cursor: `${'a'.repeat(32)}:7`, events: [], has_more: false };
  const request = vi.spyOn(apiClient, 'get').mockResolvedValue(page);
  expect(await readChatReplay('session-a', `${'a'.repeat(32)}:6`)).toEqual(page);
  expect(request).toHaveBeenCalledWith(`/chat/sessions/session-a/events?limit=100&cursor=${'a'.repeat(32)}%3A6`);
});

it('encodes a session identifier before requesting its replay', async () => {
  const request = vi.spyOn(apiClient, 'get').mockResolvedValue({ status: 'history_refresh_required', events: [], cursor: null, has_more: false });
  await readChatReplay('foreign/session', null);
  expect(request).toHaveBeenCalledWith('/chat/sessions/foreign%2Fsession/events?limit=100');
});
