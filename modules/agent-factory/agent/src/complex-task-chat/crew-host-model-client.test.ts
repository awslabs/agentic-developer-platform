import { startCrewModelAdapter } from './crew-model-adapter';
import { HOST_MODEL_REQUEST_LIMITS } from './gateway/chat-model-contract';
import { crewHostModelClient } from './crew-host-model-client';

const runId = '11111111-2222-3333-4444-555555555555';
const modelId = 'us.anthropic.example-v1:0';
const credentials = async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic-key', sessionToken: 'synthetic-session' });
const gatewayUrl = 'https://exampleapi.execute-api.us-east-1.amazonaws.com/dev';
const modelToken = async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature';

it('relays a local Crew Messages request through the signed host model route', async () => {
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).endsWith('/model-token/refresh')) return new Response(null, { status: 204 });
    expect(JSON.parse(String(init?.body))).toMatchObject({ anthropic_version: 'bedrock-2023-05-31',
      messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 });
    expect(init?.headers).toMatchObject({ 'x-agent-runid': runId });
    return new Response(JSON.stringify({ content: [{ type: 'text', text: 'world' }], stop_reason: 'end_turn',
      usage: { input_tokens: 4, output_tokens: 1 } }), { status: 200 });
  });
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId);
  try {
    const response = await fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-api-key': adapter.secret },
    body: JSON.stringify({ model: modelId, messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 }) });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ model: modelId, content: [{ type: 'text', text: 'world' }],
      usage: { input_tokens: 4, output_tokens: 1 } });
    expect(send).toHaveBeenCalledTimes(4);
  } finally { await adapter.close(); }
});

it('routes owner-shell Responses through the same signed host run as Claude Messages', async () => {
  const invoke = jest.fn();
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).endsWith('/model-token/refresh')) return new Response(null, { status: 204 });
    if (String(url).includes('/model/')) {
      invoke();
      expect(init?.headers).toMatchObject({ 'x-agent-runid': runId,
        'x-adp-crew-model-token': 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
      expect((init?.headers as Record<string, string>).authorization).toMatch(/^AWS4-HMAC-SHA256 /);
      const payload = JSON.parse(String(init?.body));
      expect(payload).toMatchObject({ anthropic_version: 'bedrock-2023-05-31',
        messages: [{ role: 'user', content: 'pong' }] });
      expect(payload).not.toHaveProperty('root_human_id');
      expect(payload).not.toHaveProperty('user_id');
      return new Response(JSON.stringify({ content: [{ type: 'text', text: 'pong' }], stop_reason: 'end_turn',
        usage: { input_tokens: 4, output_tokens: 1 } }), { status: 200 });
    }
    return new Response('{}', { status: 204 });
  });
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId, undefined, true, HOST_MODEL_REQUEST_LIMITS);
  try {
    const response = await fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', headers: {
      'content-type': 'application/json', authorization: `Bearer ${adapter.secret}` },
      body: JSON.stringify({ model: modelId, input: 'pong', client_metadata: { session_id: 'local' } }) });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ model: modelId,
      usage: { input_tokens: 4, output_tokens: 1 }, output: [{ content: [{ text: 'pong' }] }] });
    expect(invoke).toHaveBeenCalledTimes(1);
  } finally { await adapter.close(); }
});

it('denies stale authority and produces no gateway call', async () => {
  const send = jest.fn(async () => new Response('{}'));
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => 'retired', send }), modelId);
  try {
    const response = await fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-api-key': adapter.secret },
    body: JSON.stringify({ model: modelId, messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 }) });
    expect(response.status).toBe(503);
    expect(send).not.toHaveBeenCalled();
  } finally { await adapter.close(); }
});

it('relays an SDK stream through the gateway streaming route as events arrive', async () => {
  const sse = 'event: message_start\ndata: {"type":"message_start","message":{"id":"m","type":"message","role":"assistant","model":"bedrock-id","content":[],"stop_reason":null,"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
    + 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    + 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
    + 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    + 'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}\n\n'
    + 'event: message_stop\ndata: {"type":"message_stop"}\n\n';
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).endsWith('/model-token/refresh')) return new Response(null, { status: 204 });
    expect((init?.headers as Record<string, string>).accept).toBe('text/event-stream');
    return new Response(sse, { status: 200, headers: { 'content-type': 'text/event-stream' } });
  });
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId);
  try {
    const response = await fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-api-key': adapter.secret },
    body: JSON.stringify({ model: modelId, stream: true, messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 }) });
    expect(response.status).toBe(200);
    const events = await response.text();
    expect(events).toContain('event: message_start');
    expect(events).toContain(`"model":"${modelId}"`);
    expect(events).not.toContain('bedrock-id');
    expect(events).toContain('event: message_stop');
    expect(events).toContain('hello');
    expect(send.mock.calls[2][0]).toBe(`${gatewayUrl}/agent/model/us.anthropic.example-v1%3A0/invoke-with-response-stream`);
    expect(send).toHaveBeenCalledTimes(4);
    expect(send.mock.calls[3][1]?.body).toBe(JSON.stringify({ busy: false, consolidation_complete: false }));
  } finally { await adapter.close(); }
});

it('returns a refusal without a fallback or second invocation after gateway denial', async () => {
  const send = jest.fn(async () => new Response(null, { status: 403 }));
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId);
  try {
    const response = await fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-api-key': adapter.secret },
    body: JSON.stringify({ model: modelId, messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 }) });
    expect(response.status).toBe(400);
    expect(await response.text()).not.toContain('hello');
    expect(send).toHaveBeenCalledTimes(1);
  } finally { await adapter.close(); }
});

it('serves the shim\'s tools requests through the signed host tools relay (#8380)', async () => {
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).endsWith('/model-token/refresh')) return new Response(null, { status: 204 });
    expect(String(url)).toBe(`${gatewayUrl}/agent/crew/tools`);
    const request = JSON.parse(String(init?.body));
    expect((init?.headers as Record<string, string>)['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    const body = request.path === '/tools' ? { tools: [{ name: 'websearch___search' }] }
      : { request_id: request.body.request_id, status: 'confirmed' };
    return new Response(JSON.stringify({ status: request.path === '/tools' ? 200 : 202, body }), { status: 200 });
  });
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId);
  try {
    const listing = await fetch(`${adapter.baseUrl}/tools`, { headers: { authorization: `Bearer ${adapter.secret}` } });
    expect(listing.status).toBe(200);
    expect(await listing.json()).toEqual({ tools: [{ name: 'websearch___search' }] });
    const call = await fetch(`${adapter.baseUrl}/tools/call`, { method: 'POST',
      headers: { authorization: `Bearer ${adapter.secret}`, 'content-type': 'application/json' },
      body: JSON.stringify({ tool: 'websearch___search', arguments: { query: 'x' }, request_id: '00000000-0000-4000-8000-000000000099' }) });
    expect(call.status).toBe(202);
    expect(await call.json()).toEqual({ request_id: '00000000-0000-4000-8000-000000000099', status: 'confirmed' });
    expect(send).toHaveBeenCalledTimes(4);
    const unauthenticated = await fetch(`${adapter.baseUrl}/tools`);
    expect(unauthenticated.status).toBe(401);
  } finally { await adapter.close(); }
});

it('refuses tools once the host run is fenced and maps a gateway refusal to a denial (#8380)', async () => {
  const send = jest.fn(async (url: string | URL | Request) => String(url).endsWith('/model-token/refresh')
    ? new Response(null, { status: 204 }) : new Response('{}', { status: 403 }));
  const live = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId);
  try {
    const denied = await fetch(`${live.baseUrl}/tools`, { headers: { authorization: `Bearer ${live.secret}` } });
    expect(denied.status).toBe(400);
    expect(send).toHaveBeenCalledTimes(2);
  } finally { await live.close(); }
  send.mockClear();
  const fenced = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => 'retired', send }), modelId);
  try {
    const refused = await fetch(`${fenced.baseUrl}/tools`, { headers: { authorization: `Bearer ${fenced.secret}` } });
    expect(refused.status).toBe(503);
    expect(send).not.toHaveBeenCalled();
  } finally { await fenced.close(); }
});

it.each([
  ['token refresh', '/model-token/refresh', '/v1/messages', false],
  ['model invoke', '/invoke', '/v1/messages', false],
  ['streamed invoke', '/invoke-with-response-stream', '/v1/messages', true],
  ['Codex Responses invoke', '/invoke', '/v1/responses', true],
  ['tools relay', '/crew/tools', '/tools', false],
])('maps a gateway lease fence at %s to a final SDK refusal', async (_name, fencedPath, clientPath, stream) => {
  const send = jest.fn(async (url: string | URL | Request) => String(url).endsWith(fencedPath)
    ? new Response('{"detail":{"error":"lease_fenced"}}', { status: 409 })
    : new Response(null, { status: 204 }));
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId, undefined, true, HOST_MODEL_REQUEST_LIMITS);
  try {
    const request = clientPath === '/tools' ? undefined : clientPath === '/v1/responses'
      ? { model: modelId, stream: true, input: [{ type: 'message', role: 'user', content: [{ type: 'input_text', text: 'hello' }] }] }
      : { model: modelId, stream, messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 };
    const response = await fetch(`${adapter.baseUrl}${clientPath}`, { method: request ? 'POST' : 'GET',
      headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
      ...(request ? { body: JSON.stringify(request) } : {}) });
    expect(response.status).toBe(400);
    expect(response.headers.get('retry-after')).toBeNull();
    expect(await response.json()).toEqual({ type: 'error', error: { type: 'invalid_request_error', message: 'Gateway run fenced' } });
    expect(send.mock.calls.filter(([url]) => String(url).endsWith(fencedPath))).toHaveLength(1);
    if (fencedPath === '/model-token/refresh') expect(send).toHaveBeenCalledTimes(1);
  } finally { await adapter.close(); }
});

it("mints the owner's GitHub token through the signed host route and maps the gateway's refusal (#8555)", async () => {
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).endsWith('/model-token/refresh')) return new Response(null, { status: 204 });
    expect(String(url)).toBe(`${gatewayUrl}/agent/crew/github-token`);
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeUndefined();
    const headers = init?.headers as Record<string, string>;
    expect(headers['x-agent-runid']).toBe(runId);
    expect(headers['x-adp-crew-model-token']).toBe('eyJ0ZXN0IjoidG9rZW4ifQ.signature');
    expect(headers.authorization).toMatch(/^AWS4-HMAC-SHA256 /);
    expect(headers['x-amz-security-token']).toBe('synthetic-session');
    return new Response(JSON.stringify({ token: 'gho_fixture_owner', login: 'owner', expires_at: 1_800_000_000 }), { status: 200 });
  });
  const adapter = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send }), modelId);
  try {
    const response = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
    expect(response.status).toBe(200);
    const served = (await response.json()) as { token: string; login: string; expires_at: number };
    // The adapter caps a served token's hold to ten minutes even when the gateway offered more (#8748).
    expect(served).toMatchObject({ token: 'gho_fixture_owner', login: 'owner' });
    expect(served.expires_at).toBeLessThanOrEqual(Math.floor(Date.now() / 1000) + 600);
    expect(send).toHaveBeenCalledTimes(2);
  } finally { await adapter.close(); }

  const refusing = jest.fn(async (url: string | URL | Request) => String(url).endsWith('/model-token/refresh')
    ? new Response(null, { status: 204 })
    : new Response(JSON.stringify({ detail: { error: 'github_not_connected' } }), { status: 404 }));
  const refused = await startCrewModelAdapter(crewHostModelClient({ gatewayUrl, region: 'us-east-1', modelId,
    runId, credentials, modelToken, currentRun: () => runId, send: refusing }), modelId);
  try {
    const response = await fetch(`${refused.baseUrl}/github/token`, { headers: { 'x-api-key': refused.secret } });
    expect(response.status).toBe(404);
    expect(((await response.json()) as { error: { message: string } }).error.message).toContain('GitHub not connected');
  } finally { await refused.close(); }
});
