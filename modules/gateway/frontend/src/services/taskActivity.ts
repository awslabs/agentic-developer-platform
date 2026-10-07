import { apiClient, buildQueryString } from './api';
import { getAccessToken } from './auth';
import { deploymentSetting } from '@/config/runtime';
import type { InvocationListResponse } from '@/types/activity';

export function getMyTaskActivity(cursor?: string): Promise<InvocationListResponse> {
  return apiClient.get('/me/agent-invocations/tasks' + buildQueryString({ page_size: 20, last_key: cursor }));
}
export interface TaskEvent {
  task_id: string; sequence: number; type: string; timestamp: string;
  data: Record<string, unknown>;
}
export interface TaskUpdate { kind: 'snapshot' | 'event'; event?: TaskEvent; cursor?: string; status?: string }
export class TaskStreamError extends Error {
  constructor(readonly status: number) { super(`Task event stream unavailable (${status})`); }
}
/** Authenticated canonical Task stream; snapshot never advances the replay cursor. */
export async function readTaskEvents(taskId: string, cursor: string | undefined, signal: AbortSignal,
  update: (value: TaskUpdate) => void): Promise<void> {
  if (!/^tsk_[0-9a-f-]{36}$/.test(taskId)) throw new TaskStreamError(400);
  const token = getAccessToken();
  if (!token) throw new TaskStreamError(401);
  const headers: Record<string, string> = { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' };
  if (cursor) headers['Last-Event-ID'] = cursor;
  const response = await fetch(`${deploymentSetting('VITE_API_URL') || '/api'}/v1/tasks/${encodeURIComponent(taskId)}/events`,
    { headers, signal, cache: 'no-store', redirect: 'error' });
  if (!response.ok) throw new TaskStreamError(response.status);
  if (!response.body || !response.headers.get('content-type')?.startsWith('text/event-stream')) throw new TaskStreamError(503);
  const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        if (buffer.trim()) throw new TaskStreamError(503);
        return;
      }
      for (let offset = 0; offset < value.length; offset += 4096) {
        buffer += decoder.decode(value.subarray(offset, offset + 4096), { stream: true });
        let end: number;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const raw = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          if (raw.length > 131072) throw new TaskStreamError(503);
          const lines = raw.split('\n').filter(line => !line.startsWith(':'));
          if (!lines.some(line => line.trim())) continue; // Heartbeat comments are not progress.
          const kind = lines.find(line => line.startsWith('event: '))?.slice(7);
          const data = lines.find(line => line.startsWith('data: '))?.slice(6);
          const id = lines.find(line => line.startsWith('id: '))?.slice(4);
          if (!data) throw new TaskStreamError(503);
          const event = JSON.parse(data);
          if (event.task_id !== taskId || event.schema_version !== '1.0') throw new TaskStreamError(503);
          if (kind === 'snapshot' && event.frame === 'snapshot' && event.advances_last_event_id === false && !id) {
            update({ kind, status: event.status });
          } else if (kind === 'event' && Number.isSafeInteger(event.sequence) && event.sequence > 0 &&
            id === `${taskId}:${event.sequence}` && event.event_id === id && typeof event.type === 'string' &&
            typeof event.timestamp === 'string' && event.data && typeof event.data === 'object' && !Array.isArray(event.data)) {
            update({ kind, event, cursor: id });
            if (['task.completed', 'task.failed', 'task.cancelled'].includes(event.type)) return;
          } else throw new TaskStreamError(503);
        }
        if (buffer.length > 131072) throw new TaskStreamError(503);
      }
    }
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}
