import { afterEach, describe, expect, it, vi } from 'vitest';
import { readTaskEvents, getMyTaskActivity } from '@/services/taskActivity';
vi.mock('@/services/auth', () => ({ getAccessToken: () => 'test-token' }));
const task = 'tsk_12345678-1234-4123-8123-123456789abc';
const snapshot = { schema_version: '1.0', frame: 'snapshot', task_id: task, status: 'running', advances_last_event_id: false };
const event = { schema_version: '1.0', task_id: task, sequence: 2, event_id: `${task}:2`, type: 'progress.updated', timestamp: 'now', data: { message: 'hello' } };
function response(text: string) {
  return new Response(new ReadableStream({ start(controller) {
    const bytes = new TextEncoder().encode(text);
    for (let offset = 0; offset < bytes.length; offset += 7) controller.enqueue(bytes.slice(offset, offset + 7));
    controller.close();
  } }), { headers: { 'Content-Type': 'text/event-stream' } });
}
afterEach(() => vi.unstubAllGlobals());
describe('canonical Task activity', () => {
  it('reads the gateway list route, not the admission-only exact Task path', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], last_key: null })));
    vi.stubGlobal('fetch', fetch);
    await getMyTaskActivity('owned-cursor');
    expect(fetch.mock.calls[0][0]).toContain('/me/agent-invocations/tasks?');
    expect(fetch.mock.calls[0][0]).toContain('last_key=owned-cursor');
  });
  it('authenticates and resumes only real events through chunked frames', async () => {
    const fetch = vi.fn().mockResolvedValue(response(`event: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n: heartbeat\n\nid: ${task}:2\nevent: event\ndata: ${JSON.stringify(event)}\n\n`));
    vi.stubGlobal('fetch', fetch);
    const update = vi.fn();
    await readTaskEvents(task, `${task}:1`, new AbortController().signal, update);
    expect(fetch.mock.calls[0][0]).toContain(`/v1/tasks/${task}/events`);
    expect(fetch.mock.calls[0][1].headers).toMatchObject({ Authorization: 'Bearer test-token', 'Last-Event-ID': `${task}:1` });
    expect(fetch.mock.calls[0][1].redirect).toBe('error');
    expect(update.mock.calls.map(call => call[0])).toEqual([{ kind: 'snapshot', status: 'running' }, { kind: 'event', event, cursor: `${task}:2` }]);
  });
  it.each(['foreign', 'snapshot_id', 'oversize', 'bad_event_id'])('rejects %s frames', async fault => {
    let text = `id: ${task}:2\nevent: event\ndata: ${JSON.stringify(event)}\n\n`;
    if (fault === 'foreign') text = text.replaceAll(task, 'tsk_foreign');
    if (fault === 'snapshot_id') text = `id: ${task}:2\nevent: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n`;
    if (fault === 'oversize') text = 'x'.repeat(131073);
    if (fault === 'bad_event_id') text = text.replace('"event_id":"' + task + ':2"', '"event_id":"wrong"');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(text)));
    await expect(readTaskEvents(task, undefined, new AbortController().signal, vi.fn())).rejects.toThrow();
  });
});
