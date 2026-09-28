import { deploymentSetting } from '@/config/runtime';
import { getAccessToken } from './auth';

export interface LiveExplanation {
  version: 1; invocation_id: string; generation: number; sequence: number;
  timestamp: string; kind: 'explanation' | 'terminal'; payload: { text?: string };
}
export interface StreamUpdate { kind: string; cursor?: string; event?: LiveExplanation }
export class FeedError extends Error {
  constructor(readonly status: number, readonly terminal = false) { super('Live explanations unavailable'); }
}
/** Fetch streams carry auth headers; credentials never appear in URLs. */
export async function readExplanations(invocationId: string, cursor: string | undefined,
  signal: AbortSignal, update: (value: StreamUpdate) => void): Promise<void> {
  const token = getAccessToken();
  if (!token) throw new FeedError(401);
  const headers: Record<string, string> = { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' };
  if (cursor) headers['Last-Event-ID'] = cursor;
  const response = await fetch(`${deploymentSetting('VITE_API_URL') || '/api'}/activity/invocations/${encodeURIComponent(invocationId)}/agent/events`,
    { headers, signal, cache: 'no-store', redirect: 'error' });
  if (!response.ok) {
    const detail = response.status === 409 ? await response.json().catch(() => ({})) : {};
    throw new FeedError(response.status, detail.detail === 'run has reached a terminal state');
  }
  if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) throw new FeedError(503);
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      // Process bounded slices even if the browser coalesces many frames.
      for (let offset = 0; offset < value.length; offset += 4096) {
        buffer += decoder.decode(value.subarray(offset, offset + 4096), { stream: true });
        let end: number;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const raw = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          if (raw.length > 32768) throw new FeedError(503);
          const lines = raw.split('\n');
          const kind = lines.find(line => line.startsWith('event: '))?.slice(7);
          const data = lines.find(line => line.startsWith('data: '))?.slice(6);
          const id = lines.find(line => line.startsWith('id: '))?.slice(4);
          if (!kind || !data) throw new FeedError(503);
          const event = JSON.parse(data);
          if (kind === 'explanation' || kind === 'terminal') {
            if (event.version !== 1 || event.kind !== kind || event.invocation_id !== invocationId || !Number.isSafeInteger(event.generation) ||
              !Number.isSafeInteger(event.sequence) || event.sequence < 1 || typeof event.timestamp !== 'string' ||
              id !== `${invocationId}:${event.generation}:${event.sequence}` ||
              (kind === 'explanation' && typeof event.payload?.text !== 'string')) throw new FeedError(503);
            update({ kind, cursor: id, event });
            if (kind === 'terminal') return;
          } else if (['heartbeat', 'reset', 'unavailable', 'finished'].includes(kind)) update({ kind });
          else throw new FeedError(503);
        }
        if (buffer.length > 32768) throw new FeedError(503);
      }
    }
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}
