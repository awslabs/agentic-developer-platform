import { CrewHostModelRouteError, TOOLS_REQUEST_MAX_BYTES, TOOLS_TOKEN_REFRESH_MARGIN_SECONDS, invokeCrewHostModel, invokeCrewHostModelStream, invokeCrewHostResponses, invokeCrewHostTools, invokeCrewHostOrchestration, parseServerSentEvents, hostCallClient, clientUserAgentHeader, allowedCrewOrchestrationPath } from './crew-host-model-route';

const runId = '11111111-2222-3333-4444-555555555555';
const gatewayUrl = 'https://exampleapi.execute-api.us-east-1.amazonaws.com/dev';
const body = { anthropic_version: 'bedrock-2023-05-31', max_tokens: 10, messages: [{ role: 'user', content: 'hello' }] };
const credentials = async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic-key', sessionToken: 'synthetic-session' });
/** The idle mark no longer holds the reply (#8223 round 2): wait for it to be sent before asserting on it. */
async function sentCalls(send: jest.Mock, count: number) {
  for (let i = 0; i < 200 && send.mock.calls.length < count; i++) await new Promise(resolve => setTimeout(resolve, 2));
  return send.mock.calls.length;
}

describe('signed Crew host model forwarding', () => {
  const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  beforeEach(() => send.mockReset().mockImplementation(async url => String(url).includes('/model-token/refresh')
    ? new Response(null, { status: 204 }) : new Response(JSON.stringify({ content: [] }), { status: 200 })));

  const options = (currentRun: () => string = () => runId) => ({
    gatewayUrl, region: 'us-east-1', modelId: 'us.anthropic.example-v1:0', runId, currentRun, credentials, modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', send,
  });

  it('signs the approved nonstreaming model path and current run with instance credentials', async () => {
    expect(await invokeCrewHostModel(options(), body)).toEqual({ content: [] });
    expect(await sentCalls(send, 4)).toBe(4);
    expect(send.mock.calls[0][0]).toBe(`${gatewayUrl}/agent/crew/model-token/refresh`);
    expect((send.mock.calls[0][1]?.headers as Record<string, string>).authorization).toContain('Credential=TESTACCESSKEY/');
    const started = send.mock.calls[1][1];
    expect(started?.body).toBe(JSON.stringify({ busy: true, consolidation_complete: false }));
    expect((started?.headers as Record<string, string>)['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect(send.mock.calls[3][1]?.body).toBe(JSON.stringify({ busy: false, consolidation_complete: false }));
    const [url, init] = send.mock.calls[2];
    expect(url).toBe(`${gatewayUrl}/agent/model/us.anthropic.example-v1%3A0/invoke`);
    expect(init?.body).toBe(JSON.stringify(body));
    const headers = init?.headers as Record<string, string>;
    expect(headers['x-agent-runid']).toBe(runId);
    expect(headers['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect(headers.authorization).toContain('Credential=TESTACCESSKEY/');
    expect(headers.authorization).toContain('x-agent-runid');
    expect(headers['x-amz-security-token']).toBe('synthetic-session');
  });

  it('names the harness behind the call with x-adp-client-user-agent only while serving its request (#9024)', async () => {
    await hostCallClient.run({ userAgent: 'claude-cli/2.1.296 (external, cli)', session: 'user_x_account_y_session_22222222-2222-4222-8222-222222222222' }, () => invokeCrewHostModel(options(), body));
    expect(await sentCalls(send, 4)).toBe(4);
    const headers = send.mock.calls[2][1]?.headers as Record<string, string>;
    expect(headers['x-adp-client-user-agent']).toBe('claude-cli/2.1.296 (external, cli)');
    expect(headers.authorization).toContain('x-adp-client-user-agent');
    expect(headers['x-adp-client-session']).toBe('user_x_account_y_session_22222222-2222-4222-8222-222222222222');
    send.mockClear();
    await invokeCrewHostModel(options(), body);
    expect(await sentCalls(send, 4)).toBe(4);
    expect((send.mock.calls[2][1]?.headers as Record<string, string>)['x-adp-client-user-agent']).toBeUndefined();
    expect((send.mock.calls[2][1]?.headers as Record<string, string>)['x-adp-client-session']).toBeUndefined();
  });

  it('renews a token-backed busy heartbeat during a long model call', async () => {
    jest.useFakeTimers();
    try {
      let finish!: (value: Response) => void;
      send.mockImplementation(async url => String(url).includes('/model-token/refresh')
        ? new Response(null, { status: 204 }) : new Promise<Response>(resolve => { finish = resolve; }));
      const ongoing = invokeCrewHostModel(options(), body);
      await jest.advanceTimersByTimeAsync(0);
      expect(send).toHaveBeenCalledTimes(3);
      await jest.advanceTimersByTimeAsync(30_000);
      expect(send).toHaveBeenCalledTimes(5);
      expect(send.mock.calls[4][1]?.body).toBe(JSON.stringify({ busy: true, consolidation_complete: false }));
      finish(new Response(JSON.stringify({ content: [] }), { status: 200 }));
      expect(await ongoing).toEqual({ content: [] });
      for (let i = 0; i < 10 && send.mock.calls.length < 6; i++) await jest.advanceTimersByTimeAsync(0);
      expect(send.mock.calls[5][1]?.body).toBe(JSON.stringify({ busy: false, consolidation_complete: false }));
    } finally { jest.useRealTimers(); }
  });

  it('skips the refresh while the delivered token is fresh and never holds the reply for the idle mark (#8223 round 2)', async () => {
    const now = Math.floor(Date.now() / 1000);
    let idleAnswered = false;
    send.mockImplementation(async (url, init) => {
      if (!String(url).includes('/model-token/refresh')) return new Response(JSON.stringify({ content: [] }), { status: 200 });
      if (init?.body === JSON.stringify({ busy: false, consolidation_complete: false })) {
        return new Promise<Response>(() => { idleAnswered = true; /* the gateway never answers the idle mark */ });
      }
      return new Response(null, { status: 204 });
    });
    expect(await invokeCrewHostModel({ ...options(), modelTokenExpiresAt: async () => now + 60 }, body)).toEqual({ content: [] });
    expect(await sentCalls(send, 3)).toBe(3);
    expect(send.mock.calls.map(call => String(call[0]))).toEqual([
      `${gatewayUrl}/agent/crew/model-token/refresh`, `${gatewayUrl}/agent/model/us.anthropic.example-v1%3A0/invoke`,
      `${gatewayUrl}/agent/crew/model-token/refresh`]);
    expect(send.mock.calls[0][1]?.body).toBe(JSON.stringify({ busy: true, consolidation_complete: false }));
    expect(idleAnswered).toBe(true);
    send.mockClear();
    // Inside the margin the bare refresh comes back; so does an unreadable expiry.
    await invokeCrewHostModel({ ...options(), modelTokenExpiresAt: async () => now + TOOLS_TOKEN_REFRESH_MARGIN_SECONDS - 1 }, body);
    expect(await sentCalls(send, 4)).toBe(4);
    expect(send.mock.calls[0][1]?.body).toBe('{}');
    send.mockClear();
    await invokeCrewHostModel({ ...options(), modelTokenExpiresAt: async () => { throw new Error('no token yet'); } }, body);
    expect(await sentCalls(send, 4)).toBe(4);
    expect(send.mock.calls[0][1]?.body).toBe('{}');
  });

  it('heartbeats with the busy mark alone while the token stays fresh (#8223 round 2)', async () => {
    jest.useFakeTimers();
    try {
      let finish!: (value: Response) => void;
      send.mockImplementation(async url => String(url).includes('/model-token/refresh')
        ? new Response(null, { status: 204 }) : new Promise<Response>(resolve => { finish = resolve; }));
      const now = Math.floor(Date.now() / 1000);
      const ongoing = invokeCrewHostModel({ ...options(), modelTokenExpiresAt: async () => now + 3_600 }, body);
      await jest.advanceTimersByTimeAsync(0);
      expect(send).toHaveBeenCalledTimes(2); // busy, model call
      await jest.advanceTimersByTimeAsync(30_000);
      expect(send).toHaveBeenCalledTimes(3);
      expect(send.mock.calls[2][1]?.body).toBe(JSON.stringify({ busy: true, consolidation_complete: false }));
      finish(new Response(JSON.stringify({ content: [] }), { status: 200 }));
      expect(await ongoing).toEqual({ content: [] });
    } finally { jest.useRealTimers(); }
  });

  it('never sends a model call after the host service has stopped', async () => {
    const stopped = new AbortController();
    stopped.abort();
    await expect(invokeCrewHostModel(options(), body, stopped.signal)).rejects.toThrow();
    expect(send).not.toHaveBeenCalled();
  });

  it('refuses a model reply whose body finishes after its host run is fenced', async () => {
    let current = runId;
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    send.mockImplementation(async url => String(url).includes('/model-token/refresh')
      ? new Response(null, { status: 204 })
      : new Response(new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }), { status: 200 }));
    const ongoing = invokeCrewHostModel(options(() => current), body);
    const refused = expect(ongoing).rejects.toThrow('fenced');
    expect(await sentCalls(send, 3)).toBe(3);
    current = 'replaced';
    stream.enqueue(new TextEncoder().encode('{"content":[]}'));
    stream.close();
    await refused;
    expect(send).toHaveBeenCalledTimes(3);
  });

  it('aborts the model call when a renewal is refused without reporting idle', async () => {
    jest.useFakeTimers();
    try {
      send.mockImplementation(async (url, init) => {
        if (String(url).includes('/model-token/refresh')) return new Response(null, {
          status: init?.body === JSON.stringify({ busy: true, consolidation_complete: false }) && send.mock.calls.length > 3 ? 403 : 204,
        });
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('model call aborted')));
        });
      });
      const ongoing = invokeCrewHostModel(options(), body);
      const refusal = expect(ongoing).rejects.toThrow('refused (403)');
      await jest.advanceTimersByTimeAsync(0);
      expect(send).toHaveBeenCalledTimes(3);
      await jest.advanceTimersByTimeAsync(30_000);
      await refusal;
      expect(send).toHaveBeenCalledTimes(5);
      await jest.advanceTimersByTimeAsync(30_000);
      expect(send).toHaveBeenCalledTimes(5);
    } finally { jest.useRealTimers(); }
  });

  it.each([
    ['foreign endpoint', { gatewayUrl: 'https://elsewhere.example/dev' }],
    ['path injection', { modelId: '../other' }],
    ['missing stage', { gatewayUrl: 'https://exampleapi.execute-api.us-east-1.amazonaws.com/' }],
    ['wrong region', { region: 'us-west-2' }],
  ])('rejects %s before transport', async (_name, change) => {
    await expect(invokeCrewHostModel({ ...options(), ...change }, body)).rejects.toThrow('unavailable');
    expect(send).not.toHaveBeenCalled();
  });

  it('refuses revoked run and missing temporary credentials before transport', async () => {
    await expect(invokeCrewHostModel(options(() => 'revoked'), body)).rejects.toThrow('unavailable');
    await expect(invokeCrewHostModel({ ...options(), credentials: async () => ({ accessKeyId: 'id', secretAccessKey: 'key' }) }, body))
      .rejects.toThrow('unavailable');
    expect(send).not.toHaveBeenCalled();
  });

  it('does not retry a denied call or return a result after the local receipt changes', async () => {
    send.mockResolvedValueOnce(new Response(null, { status: 403 }));
    await expect(invokeCrewHostModel(options(), body)).rejects.toThrow('refused (403)');
    expect(send).toHaveBeenCalledTimes(1);
    let current = runId;
    send.mockReset().mockImplementation(async () => {
      current = 'revoked';
      return new Response(null, { status: 204 });
    });
    await expect(invokeCrewHostModel(options(() => current), body)).rejects.toThrow('fenced');
    expect(send).toHaveBeenCalledTimes(1);
  });
});

describe('signed Crew host tools relay (#8380)', () => {
  const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  const options = (currentRun: () => string = () => runId) => ({
    gatewayUrl, region: 'us-east-1', modelId: 'us.anthropic.example-v1:0', runId, currentRun, credentials,
    modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', send,
  });
  beforeEach(() => send.mockReset().mockImplementation(async url => String(url).includes('/model-token/refresh')
    ? new Response(null, { status: 204 })
    : new Response(JSON.stringify({ status: 200, body: { tools: [{ name: 'websearch___search' }] } }), { status: 200 })));

  it('refreshes the token, then signs the relay with the run and token for the shim request', async () => {
    const reply = await invokeCrewHostTools(options(), 'POST', '/tools/call', { tool: 'websearch___search', arguments: { query: 'x' } });
    expect(reply).toEqual({ status: 200, body: { tools: [{ name: 'websearch___search' }] } });
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0][0]).toBe(`${gatewayUrl}/agent/crew/model-token/refresh`);
    const [url, init] = send.mock.calls[1];
    expect(url).toBe(`${gatewayUrl}/agent/crew/tools`);
    expect(JSON.parse(String(init?.body))).toEqual({ method: 'POST', path: '/tools/call', body: { tool: 'websearch___search', arguments: { query: 'x' } } });
    const headers = init?.headers as Record<string, string>;
    expect(headers['x-agent-runid']).toBe(runId);
    expect(headers['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect(headers.authorization).toContain('Credential=TESTACCESSKEY/');
    expect(headers.authorization).toContain('x-adp-crew-model-token');
    expect(headers['x-amz-security-token']).toBe('synthetic-session');
  });

  it('skips the refresh round trip while the delivered token is fresh, and refreshes when it is not (#8223 Dev Box tool latency)', async () => {
    const now = Math.floor(Date.now() / 1000);
    const fresh = await invokeCrewHostTools({ ...options(), modelTokenExpiresAt: async () => now + 60 }, 'GET', '/tools');
    expect(fresh).toEqual({ status: 200, body: { tools: [{ name: 'websearch___search' }] } });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toBe(`${gatewayUrl}/agent/crew/tools`);
    send.mockClear();
    await invokeCrewHostTools({ ...options(), modelTokenExpiresAt: async () => now + TOOLS_TOKEN_REFRESH_MARGIN_SECONDS - 1 }, 'GET', '/tools');
    expect(send.mock.calls.map(call => String(call[0]))).toEqual([`${gatewayUrl}/agent/crew/model-token/refresh`, `${gatewayUrl}/agent/crew/tools`]);
    send.mockClear();
    // An unreadable expiry (no token yet, stale file) falls back to the refresh, never to a failure.
    await invokeCrewHostTools({ ...options(), modelTokenExpiresAt: async () => { throw new Error('no token'); } }, 'GET', '/tools');
    expect(send.mock.calls.map(call => String(call[0]))).toEqual([`${gatewayUrl}/agent/crew/model-token/refresh`, `${gatewayUrl}/agent/crew/tools`]);
  });

  it('treats an expiry reader that throws synchronously as not fresh and refreshes (#8223 cause 55)', async () => {
    // The host entrypoint's reader evaluated the token file *before* any promise existed, so an expired
    // file threw synchronously past the `.catch`: on dev 2026-10-22 every call failed locally in 5 ms
    // once the pushed 60 s token lapsed. A synchronous throw must take the refresh path, not fail.
    const expired = (() => { throw new Error('Crew model token expired or replaced'); }) as unknown as () => Promise<number>;
    expect(await invokeCrewHostTools({ ...options(), modelTokenExpiresAt: expired }, 'GET', '/tools'))
      .toEqual({ status: 200, body: { tools: [{ name: 'websearch___search' }] } });
    expect(send.mock.calls.map(call => String(call[0]))).toEqual([`${gatewayUrl}/agent/crew/model-token/refresh`, `${gatewayUrl}/agent/crew/tools`]);
    send.mockClear();
    send.mockImplementation(async url => String(url).includes('/model-token/refresh') || String(url).includes('/activity')
      ? new Response(null, { status: 204 }) : new Response(JSON.stringify({ content: [] }), { status: 200 }));
    expect(await invokeCrewHostModel({ ...options(), modelTokenExpiresAt: expired }, body)).toEqual({ content: [] });
    expect(String(send.mock.calls[0][0])).toBe(`${gatewayUrl}/agent/crew/model-token/refresh`);
  });

  it('relays a tool call close to 1 MiB and refuses one above it before signing (#8223 cause 36)', async () => {
    expect(TOOLS_REQUEST_MAX_BYTES).toBe(1_048_576);
    const large = { tool: 'code_interpreter___file', arguments: { content: 'x'.repeat(TOOLS_REQUEST_MAX_BYTES - 4_096) } };
    const reply = await invokeCrewHostTools(options(), 'POST', '/tools/call', large);
    expect(reply.status).toBe(200);
    expect(send.mock.calls[1][0]).toBe(`${gatewayUrl}/agent/crew/tools`);
    send.mockClear();
    const tooLarge = { tool: 'code_interpreter___file', arguments: { content: 'x'.repeat(TOOLS_REQUEST_MAX_BYTES) } };
    await expect(invokeCrewHostTools(options(), 'POST', '/tools/call', tooLarge)).rejects.toThrow('Crew tools request too large');
    expect(send.mock.calls.map(call => String(call[0]))).not.toContain(`${gatewayUrl}/agent/crew/tools`);
  });

  it('never signs a request outside the tools routes or for a fenced run', async () => {
    await expect(invokeCrewHostTools(options(), 'GET', '/internal/tools')).rejects.toThrow('Crew tools route unavailable');
    await expect(invokeCrewHostTools(options(), 'GET', '/tools', { unexpected: true })).rejects.toThrow('Crew tools route unavailable');
    await expect(invokeCrewHostTools(options(() => 'retired'), 'GET', '/tools')).rejects.toThrow('unavailable');
    expect(send).not.toHaveBeenCalled();
  });

  it('surfaces a gateway refusal of the host as a route error and an odd reply as invalid', async () => {
    send.mockImplementation(async url => String(url).includes('/model-token/refresh')
      ? new Response(null, { status: 204 }) : new Response('{"error":"crew_model_credential_denied"}', { status: 403 }));
    await expect(invokeCrewHostTools(options(), 'GET', '/tools')).rejects.toBeInstanceOf(CrewHostModelRouteError);
    send.mockImplementation(async () => new Response(null, { status: 409 }));
    await expect(invokeCrewHostTools(options(), 'GET', '/tools')).rejects.toMatchObject({ status: 409 });
    send.mockImplementation(async url => String(url).includes('/model-token/refresh')
      ? new Response(null, { status: 204 }) : new Response(JSON.stringify({ tools: [] }), { status: 200 }));
    await expect(invokeCrewHostTools(options(), 'GET', '/tools')).rejects.toThrow();
  });
});


describe('signed Crew host model streaming', () => {
  const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  const options = (currentRun: () => string = () => runId) => ({
    gatewayUrl, region: 'us-east-1', modelId: 'us.anthropic.example-v1:0', runId, currentRun, credentials,
    modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', send,
  });
  const frames = [
    'event: message_start\ndata: {"type":"message_start","message":{"id":"m","model":"x","usage":{"input_tokens":1,"output_tokens":0}}}\n\n',
    ': keepalive\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"pong"}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
  ];
  beforeEach(() => send.mockReset());

  it('signs the streaming route with accept text/event-stream and yields events as frames arrive, then marks idle', async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    send.mockImplementation(async url => String(url).includes('/model-token/refresh')
      ? new Response(null, { status: 204 })
      : new Response(new ReadableStream<Uint8Array>({ start(c) { controller = c; } }), { status: 200, headers: { 'content-type': 'text/event-stream' } }));
    const iterator = invokeCrewHostModelStream(options(), body)[Symbol.asyncIterator]();
    const first = iterator.next();
    for (let i = 0; i < 100 && !controller; i++) await new Promise(resolve => setTimeout(resolve, 5));
    expect(send).toHaveBeenCalledTimes(3);
    const [url, init] = send.mock.calls[2];
    expect(url).toBe(`${gatewayUrl}/agent/model/us.anthropic.example-v1%3A0/invoke-with-response-stream`);
    const headers = init?.headers as Record<string, string>;
    expect(headers.accept).toBe('text/event-stream');
    expect(headers['x-agent-runid']).toBe(runId);
    expect(headers['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect(headers.authorization).toContain('Credential=TESTACCESSKEY/');
    expect(headers.authorization).toContain('accept;');
    controller.enqueue(encoder.encode(frames[0]));
    expect((await first).value.event).toBe('message_start');
    expect(send).toHaveBeenCalledTimes(3); // not idle yet: the stream is open
    controller.enqueue(encoder.encode(frames[1] + frames[2]));
    expect(JSON.parse((await iterator.next()).value.data)).toMatchObject({ delta: { text: 'pong' } });
    controller.enqueue(encoder.encode(frames[3]));
    controller.close();
    expect((await iterator.next()).value.event).toBe('message_stop');
    expect((await iterator.next()).done).toBe(true);
    expect(await sentCalls(send, 4)).toBe(4);
    expect(send.mock.calls[3][1]?.body).toBe(JSON.stringify({ busy: false, consolidation_complete: false }));
  });

  it('refuses a stream whose run is fenced after its last event but before completion', async () => {
    let current = runId;
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    send.mockImplementation(async url => String(url).includes('/model-token/refresh')
      ? new Response(null, { status: 204 })
      : new Response(new ReadableStream<Uint8Array>({ start(stream) { controller = stream; } }), { status: 200 }));
    const iterator = invokeCrewHostModelStream(options(() => current), body);
    const first = iterator.next();
    expect(await sentCalls(send, 3)).toBe(3);
    controller.enqueue(new TextEncoder().encode(frames[3]));
    expect((await first).value.event).toBe('message_stop');
    const last = iterator.next();
    current = 'replaced';
    controller.close();
    await expect(last).rejects.toThrow('fenced');
    expect(send).toHaveBeenCalledTimes(3);
  });

  it('throws the route refusal before any event and never marks idle', async () => {
    send.mockImplementation(async url => String(url).includes('/model-token/refresh')
      ? new Response(null, { status: 204 }) : new Response('denied', { status: 403 }));
    await expect(invokeCrewHostModelStream(options(), body).next()).rejects.toThrow('refused (403)');
    expect(send).toHaveBeenCalledTimes(3);
  });

  it('cancels the upstream body when the consumer stops early', async () => {
    let cancelled = false;
    send.mockImplementation(async (url, init) => {
      if (String(url).includes('/model-token/refresh')) return new Response(null, { status: 204 });
      const stream = new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(new TextEncoder().encode(frames[0])); },
        cancel() { cancelled = true; },
      });
      init?.signal?.addEventListener('abort', () => { cancelled = true; });
      return new Response(stream, { status: 200 });
    });
    const iterator = invokeCrewHostModelStream(options(), body)[Symbol.asyncIterator]();
    expect((await iterator.next()).value.event).toBe('message_start');
    await iterator.return?.(undefined);
    expect(cancelled).toBe(true);
    expect(send).toHaveBeenCalledTimes(3); // an interrupted stream is not a completed call: no idle mark
  });
});

describe('Server-Sent Event parsing', () => {
  async function* chunks(...parts: string[]) { for (const part of parts) yield new TextEncoder().encode(part); }

  it('reassembles events split across chunks, joins multi-line data, drops comments and tolerates CRLF', async () => {
    const parsed = [];
    for await (const event of parseServerSentEvents(chunks('event: a\ndata: {"x":', '1}\n\n: keep\r\n\r\nevent: b\r\ndata: l1\r\ndata: l2\r\n\r\ndata: tail'))) parsed.push(event);
    expect(parsed).toEqual([{ event: 'a', data: '{"x":1}' }, { event: 'b', data: 'l1\nl2' }, { event: 'message', data: 'tail' }]);
  });
});


describe('pre-call resilience (#8223 cause 48)', () => {
  const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  const options = { gatewayUrl, region: 'us-east-1', modelId: 'us.anthropic.example-v1:0', runId, currentRun: () => runId, credentials,
    modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', send };
  beforeEach(() => send.mockReset());

  it('retries a dropped or 5xx token refresh and busy mark once, then completes the call', async () => {
    let refreshes = 0;
    send.mockImplementation(async (url, init) => {
      if (String(url).includes('/model-token/refresh')) {
        refreshes += 1;
        if (refreshes === 1) throw new TypeError('fetch failed');
        if (init?.body !== '{}' && refreshes === 3) return new Response(null, { status: 503 });
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify({ content: [] }), { status: 200 });
    });
    expect(await invokeCrewHostModel(options, body)).toEqual({ content: [] });
    // refresh (failed, retried), busy (503, retried), model call, idle
    expect(await sentCalls(send, 6)).toBe(6);
  });

  it('does not retry a refusal of the refresh', async () => {
    send.mockImplementation(async () => new Response(null, { status: 403 }));
    await expect(invokeCrewHostModel(options, body)).rejects.toThrow('refused (403)');
    expect(send).toHaveBeenCalledTimes(1);
  });
});


describe("the owner's per-call model choice (dev 2026-10-25)", () => {
  const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  const options = () => ({ gatewayUrl, region: 'us-east-1', modelId: 'us.anthropic.example-v1:0', runId, currentRun: () => runId, credentials,
    modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', modelTokenExpiresAt: async () => Math.floor(Date.now() / 1000) + 60, send });
  beforeEach(() => send.mockReset().mockImplementation(async url => String(url).includes('/model-token/refresh')
    ? new Response(null, { status: 204 })
    : new Response(JSON.stringify({ content: [{ type: 'text', text: 'pong' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } }), { status: 200 })));

  it('calls the gateway for the named model instead of the run default, and refuses a malformed id before transport', async () => {
    await invokeCrewHostModel(options(), { max_tokens: 10, messages: [] }, undefined, 'global.anthropic.claude-opus-5-5');
    const modelCalls = send.mock.calls.map(call => String(call[0])).filter(url => url.includes('/agent/model/'));
    expect(modelCalls).toEqual([`${gatewayUrl}/agent/model/global.anthropic.claude-opus-5-5/invoke`]);
    send.mockClear();
    await expect(invokeCrewHostModel(options(), { max_tokens: 10, messages: [] }, undefined, 'bad model/../x')).rejects.toThrow('unavailable');
    expect(send).not.toHaveBeenCalled();
  });
});

describe('Codex Responses passthrough for GPT models (dev 2026-10-26)', () => {
  const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
  const options = () => ({ gatewayUrl, region: 'us-east-1', modelId: 'us.anthropic.example-v1:0', runId, currentRun: () => runId, credentials,
    modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', modelTokenExpiresAt: async () => Math.floor(Date.now() / 1000) + 60, send });
  beforeEach(() => send.mockReset().mockImplementation(async url => String(url).includes('/model-token/refresh')
    ? new Response(null, { status: 204 })
    : new Response('{"output_text":"pong"}', { status: 200, headers: { 'content-type': 'application/json' } })));

  it("signs the owner's Responses request to the gateway's OpenAI passthrough with the run and model token", async () => {
    const reply = await invokeCrewHostResponses(options(), { model: 'openai.gpt-6-astra', input: 'pong', stream: false });
    expect(reply.status).toBe(200);
    expect(reply.contentType).toBe('application/json');
    expect(await new Response(reply.body).text()).toBe('{"output_text":"pong"}');
    await reply.finish(true);
    const [url, init] = send.mock.calls.find(call => String(call[0]).includes('/agent/openai/'))!;
    expect(url).toBe(`${gatewayUrl}/agent/openai/v1/responses`);
    expect(JSON.parse(String(init?.body))).toEqual({ model: 'openai.gpt-6-astra', input: 'pong', stream: false });
    const headers = init?.headers as Record<string, string>;
    expect(headers['x-agent-runid']).toBe(runId);
    expect(headers['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect(headers.authorization).toContain('Credential=TESTACCESSKEY/');
  });

  it('returns upstream 4xx bodies for Codex and throws on host refusals', async () => {
    send.mockImplementation(async url => String(url).includes('/model-token/refresh') ? new Response(null, { status: 204 })
      : new Response('{"error":{"message":"model not served"}}', { status: 400, headers: { 'content-type': 'application/json' } }));
    const invalid = await invokeCrewHostResponses(options(), { model: 'openai.nope', input: 'x' });
    expect(invalid.status).toBe(400);
    await invalid.finish(false);
    send.mockImplementation(async url => String(url).includes('/model-token/refresh') ? new Response(null, { status: 204 }) : new Response(null, { status: 403 }));
    await expect(invokeCrewHostResponses(options(), { model: 'openai.gpt-6-astra', input: 'x' })).rejects.toThrow('refused (403)');
  });
});


describe('client user agent on host calls (#9024)', () => {
  it('sends the harness user agent only while serving a request that carries one, header-safe', () => {
    expect(clientUserAgentHeader('claude-cli/2.1.296 (external, cli)')).toBe('claude-cli/2.1.296 (external, cli)');
    expect(clientUserAgentHeader(['codex-tui/0.164.0 (Ubuntu)\r\nX-Injected: 1'])).toBe('codex-tui/0.164.0 (Ubuntu) X-Injected: 1');
    expect(clientUserAgentHeader('x'.repeat(300))).toHaveLength(256);
    expect(clientUserAgentHeader(undefined)).toBeUndefined();
    expect(clientUserAgentHeader('\u00e9\u00e9')).toBeUndefined();
    expect(hostCallClient.getStore()).toBeUndefined();
    hostCallClient.run({ userAgent: 'codex-tui/0.164.0' }, () => expect(hostCallClient.getStore()?.userAgent).toBe('codex-tui/0.164.0'));
  });
});


describe('signed Crew host draft registration', () => {
  it('signs owner reads and draft registration with the run and model token', async () => {
    const send = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ flow_id: 'draft-id' }), { status: 201 }));
    const options = { gatewayUrl, region: 'us-east-1', modelId: 'unused', runId, currentRun: () => runId,
      credentials, modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature',
      modelTokenExpiresAt: async () => Math.floor(Date.now() / 1000) + 60, send };
    expect(await invokeCrewHostOrchestration(options, 'GET', '/orchestration/flows?limit=25&offset=0'))
      .toEqual({ status: 200, body: { items: [] } });
    expect(await invokeCrewHostOrchestration(options, 'POST', '/orchestration/flows/drafts', { flow_slug: 'draft' }))
      .toEqual({ status: 201, body: { flow_id: 'draft-id' } });
    expect(send.mock.calls.map(([url]) => url)).toEqual([
      `${gatewayUrl}/agent/crew/orchestration/flows?limit=25&offset=0`,
      `${gatewayUrl}/agent/crew/orchestration/flows/drafts`,
    ]);
    for (const [, init] of send.mock.calls) {
      const headers = init?.headers as Record<string, string>;
      expect(headers['x-agent-runid']).toBe(runId);
      expect(headers['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
      expect(headers.authorization).toContain('Credential=TESTACCESSKEY/');
      expect(headers.authorization).toContain('x-adp-crew-model-token');
    }
    expect(send.mock.calls[0][1]?.body).toBeUndefined();
    expect(send.mock.calls[1][1]?.body).toBe(JSON.stringify({ flow_slug: 'draft' }));
  });
});

describe('owner-session relay (#9640)', () => {
  it('relays only POST /orchestration/owner-session without query parameters', () => {
    expect(allowedCrewOrchestrationPath('POST', '/orchestration/owner-session')).toBe(true);
    expect(allowedCrewOrchestrationPath('GET', '/orchestration/owner-session')).toBe(false);
    expect(allowedCrewOrchestrationPath('POST', '/orchestration/owner-session?scope=all')).toBe(false);
    expect(allowedCrewOrchestrationPath('POST', '/orchestration/owner-session/refresh')).toBe(false);
  });
});

describe('host-run orchestration read allowlist (#9522)', () => {
  it('admits the Product profile and Project budget reads the adp-flow preflight needs, GET only', () => {
    expect(allowedCrewOrchestrationPath('GET', '/orchestration/products/p-1/projects/j-1')).toBe(true);
    expect(allowedCrewOrchestrationPath('GET', '/orchestration/projects/j-1/budget')).toBe(true);
    expect(allowedCrewOrchestrationPath('POST', '/orchestration/products/p-1/projects/j-1')).toBe(false);
    expect(allowedCrewOrchestrationPath('PUT', '/orchestration/projects/j-1/budget')).toBe(false);
    expect(allowedCrewOrchestrationPath('GET', '/orchestration/projects/j-1/budget?period=total')).toBe(false);
    expect(allowedCrewOrchestrationPath('GET', '/orchestration/products/p-1/projects/j-1/sources')).toBe(true);
    expect(allowedCrewOrchestrationPath('GET', '/orchestration/projects/j-1')).toBe(false);
  });
});
