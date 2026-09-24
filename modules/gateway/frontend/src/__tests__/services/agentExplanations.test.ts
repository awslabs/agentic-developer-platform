import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readExplanations } from '@/services/agentExplanations';
vi.mock('@/services/auth', () => ({ getAccessToken: () => 'browser-secret' }));
vi.mock('@/config/runtime', () => ({ deploymentSetting: () => '/api' }));
const event = { version: 1, invocation_id: 'run', generation: 1, sequence: 1, timestamp: 'now', kind: 'explanation', payload: { text: 'Mechanism 界' } };
beforeEach(() => vi.unstubAllGlobals());
function serve(raw: string, fragment = false) {
  const bytes = new TextEncoder().encode(raw);
  const cancel = vi.fn();
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
    start(controller) {
      if (fragment) for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      else controller.enqueue(bytes);
      controller.close();
    }, cancel,
  }), { headers: { 'content-type': 'text/event-stream' } })));
  return cancel;
}
describe('authenticated SSE reader', () => {
  it('handles UTF8 split across chunks and sends cursor only in headers', async () => {
    serve(`id: run:1:1\nevent: explanation\ndata: ${JSON.stringify(event)}\n\n`, true);
    const receive = vi.fn();
    await readExplanations('run', 'run:1:0', new AbortController().signal, receive);
    expect(receive).toHaveBeenCalledWith({ kind: 'explanation', cursor: 'run:1:1', event });
    expect(fetch).toHaveBeenCalledWith('/api/activity/invocations/run/agent/events', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer browser-secret', 'Last-Event-ID': 'run:1:0' }),
    }));
  });
  it('rejects foreign event identity before rendering it', async () => {
    serve(`id: other:1:1\nevent: explanation\ndata: ${JSON.stringify({ ...event, invocation_id: 'other' })}\n\n`);
    const receive = vi.fn();
    await expect(readExplanations('run', undefined, new AbortController().signal, receive)).rejects.toThrow();
    expect(receive).not.toHaveBeenCalled();
  });
  it('bounds incomplete frames', async () => {
    serve('x'.repeat(40000));
    await expect(readExplanations('run', undefined, new AbortController().signal, vi.fn())).rejects.toThrow();
  });
});
