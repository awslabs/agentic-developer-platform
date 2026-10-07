import { setTimeout as sleep } from 'node:timers/promises';
import type { ChatDataClient } from './gateway/chat-data-client';

type Scope = { run_id: string; session_id: string };

export async function maintainSessionLease(
  client: Pick<ChatDataClient, 'renewSession'>,
  scope: Scope,
  signal: AbortSignal,
  wait: (signal: AbortSignal) => Promise<void> = async cancellation => { await sleep(20_000, undefined, { signal: cancellation }); },
): Promise<void> {
  while (!signal.aborted) {
    try { await wait(signal); }
    catch (error) {
      if (signal.aborted) return;
      throw error;
    }
    if (signal.aborted) return;
    const renewed = await client.renewSession();
    if (renewed.run_id !== scope.run_id || renewed.session_id !== scope.session_id || renewed.session_mode !== 'persistent') {
      throw new Error('Chat persistent sandbox binding changed');
    }
  }
}
