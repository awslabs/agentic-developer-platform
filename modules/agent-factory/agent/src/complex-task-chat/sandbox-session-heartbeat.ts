import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';

export async function withSessionHeartbeat(
  client: ChatDataClient,
  work: (signal: AbortSignal) => Promise<void>,
  signal?: AbortSignal,
  intervalMs = 20_000,
): Promise<void> {
  signal?.throwIfAborted();
  const mode = await client.sessionMode();
  if (mode !== 'persistent') {
    await work(signal ?? new AbortController().signal);
    return;
  }
  const binding = await client.renewSession();
  if (binding.session_mode !== 'persistent') throw new ChatDataError('scope_mismatch');
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  let stopped = false;
  let timer: NodeJS.Timeout | undefined;
  let pulse: Promise<void> | undefined;
  let failure: unknown;
  const heartbeat = async () => {
    try {
      const renewed = await client.renewSession();
      const state = await client.sessionState();
      if (renewed.run_id !== binding.run_id || renewed.session_id !== binding.session_id ||
          renewed.session_mode !== 'persistent' || state.mode !== 'persistent' || state.health !== 'active') {
        throw new ChatDataError('scope_mismatch');
      }
    } catch (error) {
      failure = error;
      controller.abort(error);
      return;
    }
    if (!stopped) timer = setTimeout(() => { pulse = heartbeat(); }, intervalMs);
  };
  timer = setTimeout(() => { pulse = heartbeat(); }, intervalMs);
  try {
    await work(controller.signal);
    if (failure) throw failure;
  } finally {
    stopped = true;
    if (timer) clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    await pulse;
    if (failure) throw failure;
  }
}
