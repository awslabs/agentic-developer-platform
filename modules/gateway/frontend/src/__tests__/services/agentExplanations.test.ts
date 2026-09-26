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

// Capture native fetch before the global setup starts MSW. The only network
// requests below go to temporary loopback fixtures, never the configured API.
const nativeFetch = globalThis.fetch;
for (const origin of ['same-origin', 'cross-origin']) {
for (const status of [200, 301, 302, 303, 307, 308]) {
  it(`HTTP ${status} ${origin} accepts only the direct native-fetch event source`, async () => {
    const { createServer } = await import('node:http');
    const listen = async (server: ReturnType<typeof createServer>): Promise<number> => {
      await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
      const address = server.address();
      if (!address || typeof address === 'string') throw new Error('missing fixture port');
      return address.port;
    };
    let targetRequests = 0;
    const target = createServer((_request, response) => {
      targetRequests += 1;
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.end(`id: run:1:1\nevent: explanation\ndata: ${JSON.stringify(event)}\n\n`);
    });
    const targetPort = await listen(target);
    const source = createServer((request, response) => {
      if (request.url === '/untrusted-events') targetRequests += 1;
      if (status === 200 || request.url === '/untrusted-events') {
        response.writeHead(200, { 'content-type': 'text/event-stream' });
        response.end(`id: run:1:1\nevent: explanation\ndata: ${JSON.stringify(event)}\n\n`);
        return;
      }
      response.writeHead(status, { location: origin === 'same-origin' ? '/untrusted-events' : `http://127.0.0.1:${targetPort}/untrusted-events` });
      response.end();
    });
    const sourcePort = await listen(source);
    vi.stubGlobal('fetch', (_input: RequestInfo | URL, init?: RequestInit) =>
      nativeFetch(`http://127.0.0.1:${sourcePort}/fixture`, init));
    const receive = vi.fn();
    try {
      let rejected = false;
      try { await readExplanations('run', undefined, new AbortController().signal, receive); }
      catch { rejected = true; }
      expect({ targetRequests, renderedEvents: receive.mock.calls.length, rejected }).toEqual({
        targetRequests: 0, renderedEvents: status === 200 ? 1 : 0, rejected: status !== 200,
      });
    } finally {
      source.closeAllConnections(); target.closeAllConnections();
      await Promise.all([source, target].map(server => new Promise<void>(resolve => server.close(() => resolve()))));
    }
  });
}
}
