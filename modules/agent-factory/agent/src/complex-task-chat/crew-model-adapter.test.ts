import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { GITHUB_TOKEN_HORIZON_SECONDS, TOOLS_LISTING_FRESH_MS, TOOLS_LISTING_MAX_AGE_MS, startCrewModelAdapter } from './crew-model-adapter';
import { HOST_MODEL_REQUEST_LIMITS } from './gateway/chat-model-contract';
import { connect } from 'node:net';
import { ServerResponse } from 'node:http';

const model = 'gateway-selected-model';
const request = {
  model, max_tokens: 512, stream: true, system: [{ type: 'text', text: 'Assist the user' }],
  messages: [{ role: 'user', content: [{ type: 'text', text: 'create a file' }] }],
  tools: [{ name: 'Write', description: 'Write a file', input_schema: { type: 'object', properties: {} } }],
};

describe('loopback Responses translation', () => {
  const invokeModel = jest.fn();
  let adapter: Awaited<ReturnType<typeof startCrewModelAdapter>>;
  beforeEach(async () => {
    invokeModel.mockReset().mockResolvedValue({ modelId: model, stopReason: 'end_turn',
      content: [{ type: 'text', text: 'pong' }], usage: { input_tokens: 3, output_tokens: 1 } });
    adapter = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS);
  });
  afterEach(async () => adapter.close());
  const body = { model, instructions: 'Answer briefly', input: [{ type: 'message', role: 'user',
    content: [{ type: 'input_text', text: 'Reply pong' }] }], stream: true };

  function post(payload: unknown, secret = adapter.secret) {
    return fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', headers: {
      'content-type': 'application/json', authorization: `Bearer ${secret}` }, body: JSON.stringify(payload) });
  }

  it('streams a Responses answer with usage through the existing model client', async () => {
    const response = await post(body);
    expect(response.status).toBe(200);
    const frames = (await response.text()).split('\n\n').filter(frame => frame.startsWith('data: {'))
      .map(frame => JSON.parse(frame.slice(6)));
    expect(frames.map(frame => frame.type)).toEqual([
      'response.created', 'response.output_item.added', 'response.content_part.added',
      'response.output_text.delta', 'response.output_text.done', 'response.content_part.done',
      'response.output_item.done', 'response.completed',
    ]);
    expect(frames[3].delta).toBe('pong');
    expect(frames.at(-1).response).toMatchObject({ model, status: 'completed',
      usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 } });
    expect(invokeModel).toHaveBeenCalledWith(expect.stringMatching(/^sdk_/), {
      messages: [{ role: 'user', content: 'Reply pong' }], system: 'Answer briefly', max_tokens: 32_000,
    }, undefined, true);
  });

  it('preserves function call and result history when invoking the bound client', async () => {
    invokeModel.mockResolvedValueOnce({ modelId: model, stopReason: 'tool_use',
      content: [{ type: 'tool_use', id: 'call_1', name: 'write', input: { text: 'ok' } }],
      usage: { input_tokens: 10, output_tokens: 3 } });
    const payload = { ...body, stream: false, tools: [{ type: 'function', name: 'write',
      description: 'Write text', parameters: { type: 'object', properties: { text: { type: 'string' } } } }],
      input: [{ type: 'function_call', call_id: 'call_0', name: 'write', arguments: '{"text":"hi"}' },
        { type: 'function_call_output', call_id: 'call_0', output: 'done' }, ...body.input] };
    const response = await post(payload);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ output: [{ type: 'function_call', call_id: 'call_1',
      name: 'write', arguments: '{"text":"ok"}' }] });
    expect(invokeModel.mock.calls[0][1]).toMatchObject({
      messages: [{ role: 'assistant', content: [{ type: 'tool_use', id: 'call_0', name: 'write', input: { text: 'hi' } }] },
        { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'call_0', content: 'done' }] },
        { role: 'user', content: 'Reply pong' }],
      tools: [{ name: 'write', description: 'Write text', input_schema: payload.tools[0].parameters }],
    });
  });

  it('accepts the real CLI tool families and returns a custom call in Responses format', async () => {
    invokeModel.mockResolvedValueOnce({ modelId: model, stopReason: 'tool_use',
      content: [{ type: 'tool_use', id: 'call_1', name: 'apply_patch', input: { input: 'patch text' } }],
      usage: { input_tokens: 10, output_tokens: 3 } });
    const response = await post({ ...body, input: [
      { type: 'message', role: 'developer', content: [{ type: 'input_text', text: 'Be concise' }] },
      ...body.input], tools: [
      { type: 'custom', name: 'apply_patch', description: 'Apply a patch', format: { type: 'grammar' } },
      { type: 'function', name: 'exec_command', description: 'Run command', parameters: { type: 'object' } },
      { type: 'tool_search' }, { type: 'web_search' },
      // Families the adapter does not model are accepted and dropped (Codex 0.164.0 on dev 2026-10-23).
      { type: 'local_shell' }, { type: 'image_generation', model: 'x' }, { type: 'mcp', server_label: 'adp-tools' },
    ] });
    expect(response.status).toBe(200);
    expect(invokeModel.mock.calls[0][1].tools.map((tool: { name: string }) => tool.name)).toEqual(['apply_patch', 'exec_command']);
    const frames = (await response.text()).split('\n\n').filter(frame => frame.startsWith('data: {'))
      .map(frame => JSON.parse(frame.slice(6)));
    expect(frames.map(frame => frame.type)).toContain('response.custom_tool_call_input.done');
    expect(frames.at(-1).response.output).toMatchObject([{ type: 'custom_tool_call',
      call_id: 'call_1', name: 'apply_patch', input: 'patch text' }]);
    expect(invokeModel.mock.calls[0][1]).toMatchObject({ system: 'Answer briefly\nBe concise', tools: [
      { name: 'apply_patch', input_schema: { type: 'object', properties: { input: { type: 'string' } } } },
      { name: 'exec_command', input_schema: { type: 'object' } },
    ] });
  });

  it('rejects the wrong secret, switched model and unsupported tool before invocation', async () => {
    expect((await post(body, 'wrong')).status).toBe(401);
    // A Dev Box host forwards another model name for the gateway to judge (dev 2026-10-25); the reply must
    // then come back under that model or the turn is fenced.
    expect((await post({ ...body, model: 'unapproved' })).status).toBe(400);
    expect(invokeModel).toHaveBeenLastCalledWith(expect.stringMatching(/^sdk_/), expect.any(Object), undefined, expect.anything(), undefined, 'unapproved');
    invokeModel.mockClear();
    // A tool family the adapter does not model is accepted and dropped (Codex declares new ones per
    // release); a malformed tool entry is still refused before invocation.
    expect((await post({ ...body, tools: [{ name: 'no_type' }] })).status).toBe(400);
    expect((await post({ ...body, tools: [{ type: 'function', description: 'missing name', parameters: {} }] })).status).toBe(400);
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it.each([
    { root_human_id: 'another-owner' },
    { metadata: { root_human_id: 'another-owner' } },
    { client_metadata: { user_id: 'platform-agent', run_id: 'another-run' } },
  ])('refuses client-asserted attribution rather than billing a supplied identity: %j', async fields => {
    const response = await post({ ...body, ...fields });
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: {
      message: 'Responses run identity cannot be supplied by the client' } });
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it('refuses a fenced result instead of returning another identity or a sign-in error', async () => {
    invokeModel.mockRejectedValueOnce(new ChatDataError('scope_mismatch'));
    const response = await post(body);
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: { message: 'Gateway run fenced' } });
  });
});

describe('loopback Messages adapter', () => {
  const invokeModel = jest.fn();
  let adapter: Awaited<ReturnType<typeof startCrewModelAdapter>>;
  beforeEach(async () => {
    invokeModel.mockReset().mockResolvedValue({ modelId: model, stopReason: 'tool_use',
      content: [{ type: 'text', text: 'I will write it.' },
        { type: 'tool_use', id: 'tool_1', name: 'Write', input: { file_path: '/tmp/workspace/a.txt', content: 'hello' } }],
      usage: { input_tokens: 12, output_tokens: 9 } });
    adapter = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model);
  });
  afterEach(async () => adapter.close());

  async function post(body: unknown, secret = adapter.secret) {
    return fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-api-key': secret,
    }, body: JSON.stringify(body) });
  }

  it('streams SDK text and tool blocks from a gateway receipt in order', async () => {
    const response = await post(request);
    expect(response.status).toBe(200);
    const frames = (await response.text()).trim().split('\n\n').map(frame => JSON.parse(frame.split('\ndata: ')[1]));
    expect(frames.map(frame => frame.type)).toEqual([
      'message_start', 'content_block_start', 'content_block_delta', 'content_block_stop',
      'content_block_start', 'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop',
    ]);
    expect(frames[5].delta).toEqual({ type: 'input_json_delta', partial_json: JSON.stringify({
      file_path: '/tmp/workspace/a.txt', content: 'hello',
    }) });
    expect(invokeModel).toHaveBeenCalledWith(expect.stringMatching(/^sdk_[a-f0-9]+$/), {
      max_tokens: 512, system: 'Assist the user', messages: request.messages, tools: request.tools,
    }, undefined, true);
  });

  it('rejects unauthorized, wrong-model, oversized, and unsupported requests before invocation', async () => {
    expect((await post(request, 'wrong')).status).toBe(401);
    expect((await post({ ...request, model: 'other' })).status).toBe(400);
    expect((await post({ ...request, metadata: { operator_id: 'forged' } })).status).toBe(400);
    expect((await post({ ...request, messages: [{ role: 'user', content: 'x'.repeat(70_000) }] })).status).toBe(400);
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it('lets a host forward a user text block above 64 KiB while the sandbox profile still refuses it', async () => {
    // Kiro Crew on a Dev Box (dev 2026-11-03) composes its first turn as one user text block larger than
    // the chat sandbox's fixed 64 KiB `modelText` ceiling; the host profile allows `contentChars` (1 MB).
    const big = { ...request, messages: [{ role: 'user', content: [{ type: 'text', text: 'context '.repeat(9_000) },
      { type: 'text', text: 'y'.repeat(70_000) }] }] };
    expect((await post(big)).status).toBe(400);
    expect(invokeModel).not.toHaveBeenCalled();
    const host = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS);
    try {
      const response = await fetch(`${host.baseUrl}/v1/messages`, { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': host.secret }, body: JSON.stringify({ ...big, stream: false }) });
      expect(response.status).toBe(200);
      expect(invokeModel).toHaveBeenCalledTimes(1);
      expect(invokeModel.mock.calls[0][1].messages[0].content[1].text).toHaveLength(70_000);
      const beyond = { ...big, messages: [{ role: 'user', content: [{ type: 'text', text: 'z'.repeat(1_000_001) }] }] };
      expect((await fetch(`${host.baseUrl}/v1/messages`, { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': host.secret }, body: JSON.stringify(beyond) })).status).toBe(400);
    } finally {
      await host.close();
    }
  });

  it('accepts structured-output output_config and unknown top-level keys without forwarding them (#8223 cause 42)', async () => {
    // Observed from Claude Code 2.1.296 helper requests: anthropic-beta structured-outputs-2025-12-15 with
    // output_config.format next to effort; the strict schema answered 400 output_config:unrecognized_keys.
    const response = await post({ ...request, stream: false, thinking: { type: 'disabled' },
      output_config: { effort: 'high', format: { type: 'json_schema', schema: {} } },
      some_future_key: { nested: true }, another_flag: 1 });
    expect(response.status).toBe(200);
    expect(invokeModel).toHaveBeenCalledTimes(1);
    expect(invokeModel.mock.calls[0][1]).toEqual({
      max_tokens: 512, system: 'Assist the user', messages: request.messages, tools: request.tools,
    });
  });

  it('serves three concurrent connections instead of resetting the third (#8223 cause 41)', async () => {
    // One slow /v1/messages in flight, one idle keep-alive socket, and a GET /tools from the MCP shim.
    let release!: () => void;
    invokeModel.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({ modelId: model,
      stopReason: 'end_turn', content: [{ type: 'text', text: 'late' }], usage: { input_tokens: 1, output_tokens: 1 } }); }));
    const requestTools = jest.fn().mockResolvedValue({ status: 200, body: { tools: [{ name: 'websearch___search' }] } });
    const client = { invokeModel, requestTools } as unknown as ChatDataClient;
    const busy = await startCrewModelAdapter(client, model);
    const { port } = new URL(busy.baseUrl);
    const idle = connect({ host: '127.0.0.1', port: Number(port) });
    try {
      await new Promise<void>((resolve, reject) => { idle.once('connect', resolve); idle.once('error', reject); });
      const slow = fetch(`${busy.baseUrl}/v1/messages`, { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': busy.secret }, body: JSON.stringify({ ...request, stream: false }) });
      await new Promise(resolve => setTimeout(resolve, 50));
      const tools = await fetch(`${busy.baseUrl}/tools`, { headers: { 'x-api-key': busy.secret } });
      expect(tools.status).toBe(200);
      expect(await tools.json()).toEqual({ tools: [{ name: 'websearch___search' }] });
      expect(requestTools).toHaveBeenCalledWith('GET', '/tools', undefined);
      release();
      expect((await slow).status).toBe(200);
    } finally {
      idle.destroy();
      await busy.close();
    }
  });

  it('returns a non-streaming response with confirmed usage', async () => {
    const response = await post({ ...request, stream: false });
    expect(await response.json()).toMatchObject({ model, stop_reason: 'tool_use',
      usage: { input_tokens: 12, output_tokens: 9 } });
  });

  it('retains gateway rejection without suggesting a direct model retry', async () => {
    invokeModel.mockRejectedValueOnce(new ChatDataError('denied'));
    const response = await post(request);
    expect(response.status).toBe(400);
    expect(adapter.gatewayError).toMatchObject({ code: 'denied' });
  });

  it('normalizes validated cache hints on user, assistant, tool-use, tool-result, and tool-definition blocks', async () => {
    const cache = { cache_control: { type: 'ephemeral', ttl: '1h' } };
    const messages = [
      { role: 'user', content: [{ type: 'text', text: 'Create a file' }] },
      { role: 'assistant', content: [{ type: 'text', text: 'Writing it' },
        { type: 'tool_use', id: 'tool_1', name: 'Write', input: { content: 'hello', cache_control: 'tool input, not a hint' } }] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_1', content: 'Wrote file' }] },
    ];
    const response = await post({ ...request, stream: false,
      messages: messages.map(message => ({ ...message, content: message.content.map(block => ({ ...block, ...cache })) })),
      tools: request.tools.map(tool => ({ ...tool, ...cache })), thinking: { type: 'disabled' },
    });
    expect(response.status).toBe(200);
    expect(invokeModel.mock.calls[0][1]).toEqual({
      messages, tools: request.tools, max_tokens: 512, system: 'Assist the user',
    });
  });

  it.each([
    { type: 'persistent' }, { type: 'ephemeral', ttl: 'forever' }, { type: 'ephemeral', identity: 'forged' },
  ])('rejects unsupported cache hints before gateway invocation: %j', async cache => {
    const response = await post({ ...request, messages: [{ role: 'user',
      content: [{ type: 'text', text: 'Hello', cache_control: cache }] }] });
    expect(response.status).toBe(400);
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it.each([{ type: 'adaptive' }, { type: 'adaptive', display: 'updates' }, { type: 'enabled', budget_tokens: 4096 }])(
    'accepts thinking settings and never forwards them (#8223 cause 44): %j', async thinking => {
      // The gateway does not execute extended thinking, but Claude Code 2.1.296 asks for adaptive
      // thinking on every turn; refusing it meant no Dev Box turn could ever start. The setting is
      // dropped and the turn runs without thinking blocks.
      expect((await post({ ...request, thinking })).status).toBe(200);
      expect(invokeModel).toHaveBeenCalledTimes(1);
      expect(invokeModel.mock.calls[0][1]).not.toHaveProperty('thinking');
    },
  );

  it.each([
    { role: 'user', content: [{ type: 'text', text: 'x'.repeat(32_001), cache_control: { type: 'ephemeral' } }] },
    { role: 'user', content: [{ type: 'tool_use', id: 'tool_1', name: 'Write', input: {}, cache_control: { type: 'ephemeral' } }] },
    { role: 'user', content: [{ type: 'text', text: 'Hello', identity: 'forged', cache_control: { type: 'ephemeral' } }] },
  ])('keeps gateway content limits and roles strict after cache normalization %#', async message => {
    expect((await post({ ...request, messages: [message] })).status).toBe(400);
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it('normalizes SDK reminders and tool results without forwarding client identity or raising gateway limits', async () => {
    const response = await post({ ...request, stream: false, max_tokens: 32_000,
      system: [{ type: 'text', text: 'Assist the user', cache_control: { type: 'ephemeral' } }],
      messages: [request.messages[0], { role: 'system', content: [{ type: 'text', text: 'Tool reminder',
        cache_control: { type: 'ephemeral' } }] },
      { role: 'assistant', content: [{ type: 'tool_use', id: 'tool_1', name: 'Write', input: {} }] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_1', content: [{ type: 'text', text: 'Wrote file' }] }] }],
      metadata: { user_id: 'untrusted-sdk-user' }, thinking: { type: 'disabled' },
      context_management: { edits: [{ type: 'clear_thinking_20251015', keep: 'all' }] },
      output_config: { effort: 'high' },
    });
    expect(response.status).toBe(200);
    expect(invokeModel).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
      max_tokens: 10_000, system: 'Assist the user\nTool reminder',
      messages: [request.messages[0],
        { role: 'assistant', content: [{ type: 'tool_use', id: 'tool_1', name: 'Write', input: {} }] },
        { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_1', content: 'Wrote file' }] }],
    }), undefined, true);
    expect(JSON.stringify(invokeModel.mock.calls[0][1])).not.toContain('untrusted-sdk-user');
  });
});

describe('Crew run binding through the local Messages proxy', () => {
  const runBindingId = '00000000-0000-4000-8000-000000000001';

  test.each([runBindingId, undefined])('only forwards a gateway model call with an admitted run binding: %s', async boundRun => {
    const { createHash } = await import('node:crypto');
    const { canonicalJson } = await import('../invocability-probe/canonical-json');
    const seen: { path: string; runId?: string }[] = [];
    const realFetch = globalThis.fetch;
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
      const url = String(input);
      if (url.startsWith('http://127.0.0.1:')) return realFetch(input, options);
      const path = new URL(url).pathname;
      const header = (options?.headers as Record<string, string>)?.['x-agent-runid'];
      seen.push({ path, ...(header ? { runId: header } : {}) });
      const json = (value: unknown) => new Response(JSON.stringify(value), {
        headers: { 'content-type': 'application/json' },
      });
      if (path === '/v1/chat/data/bootstrap') return json({ capability: 'synthetic.capability',
        run_id: 'run-a', session_id: 'session-a', lease_generation: 1, attempt: 1,
        expires_at: Math.floor(Date.now() / 1000) + 120, session_mode: 'persistent' });
      if (path !== '/v1/chat/model/invoke' || header !== runBindingId) {
        return new Response(JSON.stringify({ error: 'missing admitted run binding' }), { status: 403,
          headers: { 'content-type': 'application/json' } });
      }
      const body = JSON.parse(options!.body as string);
      expect(JSON.stringify(body.request)).not.toContain('forged-operator');
      return json({ run_id: body.run_id, session_id: body.session_id,
        operation_id: body.operation_id, lease_generation: 1,
        request_digest: createHash('sha256').update(canonicalJson(body.request)).digest('hex'),
        model_id: model, status: 'confirmed', handoff: 'confirmed', reservation_status: 'settled',
        automatic_replay_permitted: false, content: [{ type: 'text', text: 'KIROCREW-ADP-OK' }],
        stop_reason: 'end_turn', usage: { input_tokens: 5, output_tokens: 3, estimated_usd: '0.001' } });
    });
    const client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', ...(boundRun ? { crewRunId: boundRun } : {}) });
    let adapter: Awaited<ReturnType<typeof startCrewModelAdapter>> | undefined;
    try {
      await client.sessionScope();
      adapter = await startCrewModelAdapter(client.withSignal(new AbortController().signal), model);
      const response = await realFetch(adapter.baseUrl + '/v1/messages', { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': adapter.secret,
      }, body: JSON.stringify({ model, max_tokens: 16, metadata: { user_id: 'forged-operator' },
        messages: [{ role: 'user', content: 'Reply with exactly: KIROCREW-ADP-OK' }] }) });
      if (boundRun) {
        expect(response.status).toBe(200);
        expect(((await response.json()) as { content: unknown }).content).toEqual([{ type: 'text', text: 'KIROCREW-ADP-OK' }]);
        expect(seen).toEqual([{ path: '/v1/chat/data/bootstrap' },
          { path: '/v1/chat/model/invoke', runId: runBindingId }]);
      } else {
        expect(response.status).toBe(400);
        expect(seen).toEqual([{ path: '/v1/chat/data/bootstrap' }]);
      }
    } finally {
      await adapter?.close();
      jest.restoreAllMocks();
    }
  });
});

describe('Dev Box host envelope (#8223 cause 36)', () => {
  // Claude Code's first request (system prompt + tool schemas) is well over 64 KiB and a session runs to
  // hundreds of messages; the chat sandbox's envelope refused it with "Messages request exceeds 65536 bytes".
  const invokeModel = jest.fn();
  let adapter: Awaited<ReturnType<typeof startCrewModelAdapter>>;
  beforeEach(async () => {
    invokeModel.mockReset().mockResolvedValue({ modelId: model, stopReason: 'end_turn',
      content: [{ type: 'text', text: 'done' }], usage: { input_tokens: 1, output_tokens: 1 } });
    adapter = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS);
  });
  afterEach(async () => adapter.close());

  async function post(body: unknown) {
    return fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
      'content-type': 'application/json', 'x-api-key': adapter.secret,
    }, body: JSON.stringify(body) });
  }

  it('accepts a 1 MiB request with a long conversation and clamps max_tokens to the host ceiling', async () => {
    const messages = Array.from({ length: 400 }, (_, i) => i % 2 === 0
      ? { role: 'user', content: `turn ${i} ` + 'x'.repeat(5_000) }
      : { role: 'assistant', content: [{ type: 'text', text: `reply ${i}` }] });
    const payload = { ...request, stream: false, max_tokens: 64_000, system: 's'.repeat(100_000), messages };
    expect(Buffer.byteLength(JSON.stringify(payload))).toBeGreaterThan(1_048_576);
    const response = await post(payload);
    expect(response.status).toBe(200);
    expect(invokeModel).toHaveBeenCalledTimes(1);
    expect(invokeModel.mock.calls[0][1]).toMatchObject({ max_tokens: HOST_MODEL_REQUEST_LIMITS.maxTokens });
    expect(invokeModel.mock.calls[0][1].messages).toHaveLength(400);
  });

  it('accepts the request shape Claude Code 2.1.296 sends on a Dev Box turn (#8223 cause 44)', async () => {
    // Captured on dev 2026-10-21: adaptive thinking with a display hint, context management, a JSON
    // user_id in metadata, cached system blocks, and more tool definitions than the 128 the host
    // envelope first allowed (built-ins plus every MCP tool the host exposes). The adapter drops the
    // thinking and output configuration and forwards the rest.
    const tools = Array.from({ length: 300 }, (_, i) => ({
      name: `mcp__adp-tools__server___tool_${i}`, description: `tool ${i}`,
      input_schema: { type: 'object', properties: { a: { type: 'string' } }, required: ['a'], additionalProperties: false },
    }));
    const response = await post({
      ...request, max_tokens: 64_000,
      metadata: { user_id: JSON.stringify({ device_id: 'd'.repeat(64), account_uuid: '', session_id: 'session' }) },
      thinking: { type: 'adaptive', display: 'updates' },
      context_management: { edits: [{ type: 'clear_thinking_20251015', keep: 'all' }] },
      output_config: { effort: 'high' },
      system: [{ type: 'text', text: 's1' }, { type: 'text', text: 's2', cache_control: { type: 'ephemeral', ttl: '1h' } }],
      messages: [{ role: 'user', content: [{ type: 'text', text: 'Reply with the single word pong.', cache_control: { type: 'ephemeral' } }] }],
      tools,
    });
    expect(response.status).toBe(200);
    expect(invokeModel).toHaveBeenCalledTimes(1);
    const forwarded = invokeModel.mock.calls[0][1];
    expect(forwarded.tools).toHaveLength(300);
    expect(forwarded).not.toHaveProperty('thinking');
    expect(forwarded).not.toHaveProperty('output_config');
    expect(forwarded).not.toHaveProperty('context_management');
    expect(forwarded.system).toEqual([{ type: 'text', text: 's1' }, { type: 'text', text: 's2', cache_control: { type: 'ephemeral', ttl: '1h' } }]);
  });

  it('forwards Claude Code\'s prompt-cache markers byte-for-byte on system, tools and message blocks', async () => {
    // Dev 2026-10-21: every host call carried ~115k input tokens with cache_read_input_tokens 0 and took
    // 4–9 s because the markers were stripped and the system prompt flattened. The gateway forwards the
    // body to Bedrock as-is for host runs, so the markers only have to survive this adapter.
    const tools = [
      { name: 'Read', description: 'Read a file', input_schema: { type: 'object', properties: {} } },
      { name: 'Write', description: 'Write a file', input_schema: { type: 'object', properties: {} }, cache_control: { type: 'ephemeral' } },
    ];
    const messages = [
      { role: 'user', content: [{ type: 'text', text: 'Create a file', cache_control: { type: 'ephemeral', ttl: '5m' } }] },
      { role: 'assistant', content: [{ type: 'tool_use', id: 'tool_1', name: 'Write', input: { path: 'a' }, cache_control: { type: 'ephemeral' } }] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool_1', content: 'Wrote file', cache_control: { type: 'ephemeral' } }] },
      { role: 'system', content: [{ type: 'text', text: 'Tool reminder', cache_control: { type: 'ephemeral' } }] },
    ];
    const response = await post({ ...request, stream: false, tools, messages,
      system: [{ type: 'text', text: 'Assist', cache_control: { type: 'ephemeral', ttl: '1h' } }] });
    expect(response.status).toBe(200);
    // The system-role reminder stays in the conversation (its marker is the history breakpoint, #8223
    // cause 51); the system prompt is forwarded exactly as sent.
    expect(invokeModel.mock.calls[0][1]).toEqual({
      max_tokens: 512, tools,
      system: [{ type: 'text', text: 'Assist', cache_control: { type: 'ephemeral', ttl: '1h' } }],
      messages,
    });
  });

  it('still refuses a malformed cache marker on the host path', async () => {
    const response = await post({ ...request, messages: [{ role: 'user',
      content: [{ type: 'text', text: 'Hello', cache_control: { type: 'persistent' } }] }] });
    expect(response.status).toBe(400);
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it('accepts a cleared thinking block with a long Bedrock signature and reports request-shape refusals (#8223 cause 50)', async () => {
    // Captured dev 2026-10-22: a thinking block with no text and a 19,328-char signature. The old 16_000 cap
    // refused it in 3 ms and Claude Code retried with a different, far larger body on every turn.
    const signature = 'S'.repeat(19_328);
    const response = await post({ ...request, stream: false, messages: [
      { role: 'user', content: [{ type: 'text', text: 'first' }] },
      { role: 'assistant', content: [{ type: 'thinking', thinking: '', signature }, { type: 'text', text: 'reply' }] },
      { role: 'user', content: [{ type: 'text', text: 'again' }] },
    ] });
    expect(response.status).toBe(200);
    expect(invokeModel).toHaveBeenCalledTimes(1);
    expect(invokeModel.mock.calls[0][1].messages[1].content[0]).toEqual({ type: 'thinking', thinking: '', signature });
    const lines: string[] = [];
    const logged = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS, line => lines.push(line));
    try {
      const refused = await fetch(`${logged.baseUrl}/v1/messages`, { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': logged.secret,
      }, body: JSON.stringify({ ...request, messages: [{ role: 'user', content: [{ type: 'bogus' }] }] }) });
      expect(refused.status).toBe(400);
    } finally { await logged.close(); }
    expect(lines.map(line => JSON.parse(line))).toEqual([expect.objectContaining({ event: 'crew_model_adapter_failure', route: 'messages', status: 400,
      message: expect.stringContaining('Unsupported Messages payload') })]);
  });

  it('forwards Claude Code\'s system-role reminders in place so the trailing cache breakpoint survives (#8223 cause 51)', async () => {
    // Shape captured on dev 2026-10-22 (gen 31): three system blocks (two marked), a 9 KB environment
    // reminder as a system-role message mid-conversation, and a final marked system-role reminder that
    // is Claude Code's "cache the whole history" breakpoint. Hoisting those into `system` moved the
    // breakpoint out of the conversation, so ~58k tokens were re-billed uncached on every turn.
    const system = [
      { type: 'text', text: 'x-anthropic-billing-header: cc_version=2.1.296' },
      { type: 'text', text: 'You are a Claude agent.', cache_control: { type: 'ephemeral' } },
      { type: 'text', text: 'Long instructions…', cache_control: { type: 'ephemeral' } },
    ];
    const messages = [
      { role: 'user', content: [{ type: 'text', text: 'first' }, { type: 'text', text: 'context ' + 'c'.repeat(12_000) }] },
      { role: 'system', content: '<system-reminder># Environment ' + 'e'.repeat(9_000) + '</system-reminder>' },
      { role: 'assistant', content: [{ type: 'thinking', thinking: '', signature: 'S'.repeat(19_328) }, { type: 'text', text: 'reply' }] },
      { role: 'user', content: [{ type: 'text', text: 'again' }] },
      { role: 'system', content: [{ type: 'text', text: '<system-reminder><total_tokens>15000000 tokens left</total_tokens></system-reminder>',
        cache_control: { type: 'ephemeral' } }] },
    ];
    const response = await post({ ...request, stream: false, system, messages, thinking: { type: 'adaptive', display: 'updates' } });
    expect(response.status).toBe(200);
    const forwarded = invokeModel.mock.calls[0][1];
    expect(forwarded.system).toEqual(system);
    expect(forwarded.messages.map((message: { role: string }) => message.role)).toEqual(['user', 'system', 'assistant', 'user', 'system']);
    expect(forwarded.messages[1]).toEqual(messages[1]);
    expect(forwarded.messages[4]).toEqual(messages[4]);
    expect(JSON.stringify(forwarded).split('cache_control').length - 1).toBe(3);
  });

  it('still refuses a request above the 8 MiB host cap with the SDK error shape, before invocation', async () => {
    const response = await post({ ...request, messages: [{ role: 'user', content: 'x'.repeat(HOST_MODEL_REQUEST_LIMITS.bodyBytes) }] });
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ type: 'error', error: { type: 'invalid_request_error',
      message: `Messages request exceeds ${HOST_MODEL_REQUEST_LIMITS.bodyBytes} bytes` } });
    expect(invokeModel).not.toHaveBeenCalled();
  });

  it('keeps the chat sandbox on its 64 KiB envelope', async () => {
    const sandbox = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model);
    try {
      const response = await fetch(`${sandbox.baseUrl}/v1/messages`, { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': sandbox.secret,
      }, body: JSON.stringify({ ...request, messages: [{ role: 'user', content: 'x'.repeat(70_000) }] }) });
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({ error: { message: 'Messages request exceeds 65536 bytes' } });
    } finally { await sandbox.close(); }
    expect(invokeModel).not.toHaveBeenCalled();
  });
});


describe('Dev Box host streaming relay', () => {
  // The gateway's invoke-with-response-stream route answers with Anthropic-format SSE; the adapter
  // forwards each event as it arrives instead of synthesising them after the whole reply.
  const invokeModel = jest.fn();
  const sse = (event: string, data: unknown) => ({ event, data: JSON.stringify(data) });
  const script = [
    sse('message_start', { type: 'message_start', message: { id: 'msg_1', type: 'message', role: 'assistant', model: 'bedrock-side-id',
      content: [], stop_reason: null, usage: { input_tokens: 115_000, cache_read_input_tokens: 114_000, output_tokens: 0 } } }),
    sse('content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }),
    sse('ping', { type: 'ping' }),
    sse('content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'po' } }),
    sse('content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'ng' } }),
    sse('content_block_stop', { type: 'content_block_stop', index: 0 }),
    sse('message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 3 } }),
    sse('message_stop', { type: 'message_stop' }),
  ];

  function controllableStream() {
    const gates: Array<() => void> = [];
    let released = 0;
    const seen: { signal?: AbortSignal; returned: boolean; requests: unknown[] } = { returned: false, requests: [] };
    async function* events(_operation: string, input: unknown, signal?: AbortSignal) {
      seen.signal = signal;
      seen.requests.push(input);
      try {
        for (const event of script) {
          await new Promise<void>(resolve => { gates.push(resolve); if (released > gates.length - 1) resolve(); });
          yield event;
        }
      } finally { seen.returned = true; }
    }
    const release = (count: number) => { released = count; gates.slice(0, count).forEach(resolve => resolve()); };
    return { events, release, seen };
  }

  async function startAdapter(invokeModelStream: unknown) {
    invokeModel.mockReset();
    return startCrewModelAdapter({ invokeModel, invokeModelStream } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS);
  }

  const post = (adapter: Awaited<ReturnType<typeof startCrewModelAdapter>>, body: unknown, signal?: AbortSignal) =>
    fetch(`${adapter.baseUrl}/v1/messages?beta=true`, { method: 'POST', signal, headers: {
      'content-type': 'application/json', 'x-api-key': adapter.secret }, body: JSON.stringify(body) });

  it('relays events incrementally, rewrites the model id, drops pings and carries usage and message_stop', async () => {
    const stream = controllableStream();
    const adapter = await startAdapter(stream.events);
    try {
      stream.release(2);
      const response = await post(adapter, { ...request, stream: true });
      expect(response.status).toBe(200);
      expect(response.headers.get('content-type')).toBe('text/event-stream');
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let received = '';
      while (!received.includes('content_block_start')) received += decoder.decode((await reader.read()).value);
      // The first events reached the SDK while the gateway stream is still open (six events pending).
      expect(stream.seen.returned).toBe(false);
      expect(received).toContain(`"model":"${model}"`);
      expect(received).not.toContain('bedrock-side-id');
      stream.release(script.length);
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        received += decoder.decode(chunk.value);
      }
      expect(received).toContain('event: message_delta');
      expect(received).toContain('"output_tokens":3');
      expect(received).toContain('"cache_read_input_tokens":114000');
      expect(received.trim().endsWith('event: message_stop\ndata: {"type":"message_stop"}')).toBe(true);
      expect(received).not.toContain('event: ping');
      expect(invokeModel).not.toHaveBeenCalled();
      expect(stream.seen.requests[0]).toMatchObject({ max_tokens: 512 });
    } finally { await adapter.close(); }
  });

  it('cancels the upstream stream when the SDK goes away mid-reply', async () => {
    const stream = controllableStream();
    const adapter = await startAdapter(stream.events);
    try {
      stream.release(2);
      const aborter = new AbortController();
      const response = await post(adapter, { ...request, stream: true }, aborter.signal);
      const reader = response.body!.getReader();
      await reader.read();
      aborter.abort();
      await expect(reader.read()).rejects.toThrow();
      for (let i = 0; i < 50 && !stream.seen.signal?.aborted; i++) await new Promise(resolve => setTimeout(resolve, 10));
      expect(stream.seen.signal?.aborted).toBe(true);
    } finally { await adapter.close(); }
  });

  it('answers a gateway refusal before the first byte with the SDK error shape', async () => {
    // eslint-disable-next-line require-yield
    async function* refused(): AsyncGenerator<never> { throw new ChatDataError('denied', 403); }
    const adapter = await startAdapter(refused);
    try {
      const response = await post(adapter, { ...request, stream: true });
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ type: 'error', error: { type: 'invalid_request_error', message: 'Gateway denied' } });
      expect(adapter.gatewayError).toMatchObject({ code: 'denied' });
    } finally { await adapter.close(); }
  });

  it('keeps synthesising events for a client without a streaming route (the chat sandbox)', async () => {
    invokeModel.mockReset().mockResolvedValue({ modelId: model, stopReason: 'end_turn',
      content: [{ type: 'text', text: 'done' }], usage: { input_tokens: 1, output_tokens: 1 } });
    const adapter = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    try {
      const response = await post(adapter, { ...request, stream: true });
      const events = await response.text();
      expect(events).toContain('event: message_start');
      expect(events).toContain('done');
      expect(invokeModel).toHaveBeenCalledTimes(1);
    } finally { await adapter.close(); }
  });
});


describe('SDK-facing failure statuses (#8223 cause 48)', () => {
  // Claude Code reads 401/403 as "not signed in"; on dev 2026-10-21 a transient local failure answered
  // as 403 latched the Claude agent adapter into a sign-out prompt for the owner's next chat.
  const invokeModel = jest.fn();
  const requestTools = jest.fn();
  const lines: string[] = [];
  let adapter: Awaited<ReturnType<typeof startCrewModelAdapter>>;
  beforeEach(async () => {
    invokeModel.mockReset();
    requestTools.mockReset();
    lines.length = 0;
    adapter = await startCrewModelAdapter({ invokeModel, requestTools } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS, line => lines.push(line));
  });
  afterEach(async () => adapter.close());
  const post = (body: unknown, key = adapter.secret) => fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
    'content-type': 'application/json', 'x-api-key': key }, body: JSON.stringify(body) });

  it.each([
    ['unavailable gateway', new ChatDataError('unavailable', 503), 503, 'api_error', 'Gateway unavailable', '2'],
    ['unexpected local error', new TypeError('fetch failed'), 503, 'api_error', 'Gateway unavailable', '2'],
    ['rate limit', new ChatDataError('rate_limited', 429, 5000), 429, 'rate_limited_error', 'Gateway rate_limited', '5'],
    ['gateway refusal', new ChatDataError('denied', 403), 400, 'invalid_request_error', 'Gateway denied', null],
    ['fenced run', new ChatDataError('scope_mismatch'), 400, 'invalid_request_error', 'Gateway run fenced', null],
  ])('%s is never a 401/403 to the SDK', async (_name, error, status, type, message, retryAfter) => {
    invokeModel.mockRejectedValue(error);
    const response = await post(request);
    expect(response.status).toBe(status);
    expect(response.headers.get('retry-after')).toBe(retryAfter);
    expect(await response.json()).toEqual({ type: 'error', error: { type, message } });
    expect(lines).toHaveLength(1);
    const logged = JSON.parse(lines[0]);
    expect(logged).toMatchObject({ event: 'crew_model_adapter_failure', route: 'messages', status });
    expect(logged.elapsed_ms).toBeGreaterThanOrEqual(0);
    expect(lines[0]).not.toContain(adapter.secret);
    expect(lines[0]).not.toContain('Assist the user');
  });

  it('names the local reason behind an unavailable answer in the journal line (#8223 cause 55)', async () => {
    invokeModel.mockRejectedValue(Object.assign(new ChatDataError('unavailable'), { cause: new Error('Crew model token expired or replaced') }));
    expect((await post(request)).status).toBe(503);
    expect(JSON.parse(lines[0])).toMatchObject({ route: 'messages', status: 503, gateway_code: 'unavailable', cause: 'Crew model token expired or replaced' });
  });

  it('keeps 401 for a wrong loopback secret (unlogged) and 400 for a malformed request (logged, #8223 cause 50)', async () => {
    expect((await post(request, 'wrong')).status).toBe(401);
    const malformed = await post({ ...request, messages: [] });
    expect(malformed.status).toBe(400);
    expect(await malformed.json()).toMatchObject({ error: { type: 'invalid_request_error' } });
    expect(invokeModel).not.toHaveBeenCalled();
    // The wrong-secret 401 stays silent; the malformed 400 is reported so a request-shape refusal
    // that makes Claude Code retry with a different body is visible in the host journal.
    expect(lines.map(line => JSON.parse(line))).toEqual([expect.objectContaining({ route: 'messages', status: 400 })]);
  });

  it('maps tools relay failures the same way and logs them', async () => {
    requestTools.mockRejectedValue(new ChatDataError('unavailable', 503));
    const unavailable = await fetch(`${adapter.baseUrl}/tools`, { headers: { 'x-api-key': adapter.secret } });
    expect(unavailable.status).toBe(503);
    expect(unavailable.headers.get('retry-after')).toBe('2');
    expect(await unavailable.json()).toEqual({ type: 'error', error: { type: 'api_error', message: 'Crew tools unavailable' } });
    requestTools.mockRejectedValue(new ChatDataError('denied', 403));
    expect((await fetch(`${adapter.baseUrl}/tools`, { headers: { 'x-api-key': adapter.secret } })).status).toBe(400);
    expect(lines.map(line => JSON.parse(line).route)).toEqual(['tools', 'tools']);
  });

  it('answers a streaming refusal before the first byte with the mapped status', async () => {
    // eslint-disable-next-line require-yield
    async function* unavailable(): AsyncGenerator<never> { throw new ChatDataError('unavailable', 503); }
    const streaming = await startCrewModelAdapter({ invokeModel, invokeModelStream: unavailable } as unknown as ChatDataClient, model,
      undefined, true, HOST_MODEL_REQUEST_LIMITS, line => lines.push(line));
    try {
      const response = await fetch(`${streaming.baseUrl}/v1/messages`, { method: 'POST', headers: {
        'content-type': 'application/json', 'x-api-key': streaming.secret }, body: JSON.stringify({ ...request, stream: true }) });
      expect(response.status).toBe(503);
      expect(lines).toHaveLength(1);
    } finally { await streaming.close(); }
  });
});

describe("the owner's GitHub token for the box's gh/git (#8555)", () => {
  const model = 'us.anthropic.example-v1:0';
  const minted = { token: 'gho_fixture_owner', login: 'owner', expires_at: Math.floor(Date.now() / 1000) + 600 };

  it('caps an oversized gateway expiry and re-mints before a paused grant can remain cached ten minutes', async () => {
    let clock = Date.now();
    const clockSpy = jest.spyOn(Date, 'now').mockImplementation(() => clock);
    const claimed = { ...minted, expires_at: Math.floor(clock / 1000) + 3600 };
    const requestGithubToken = jest.fn().mockResolvedValueOnce(claimed).mockRejectedValueOnce(new ChatDataError('denied', 403));
    const adapter = await startCrewModelAdapter({ invokeModel: jest.fn(), requestGithubToken } as unknown as ChatDataClient, model);
    try {
      const first = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
      expect(first.status).toBe(200);
      const initial = await first.json() as { expires_at: number };
      expect(initial.expires_at).toBe(Math.floor(clock / 1000) + GITHUB_TOKEN_HORIZON_SECONDS);
      clock += 301_000;
      const refused = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
      expect(refused.status).toBe(403);
      expect(requestGithubToken).toHaveBeenCalledTimes(2);
    } finally { await adapter.close(); clockSpy.mockRestore(); }
  });

  it('serves the token from the signed mint, reuses it inside its horizon and never writes it anywhere', async () => {
    const requestGithubToken = jest.fn().mockResolvedValue(minted);
    const adapter = await startCrewModelAdapter({ invokeModel: jest.fn(), requestGithubToken } as unknown as ChatDataClient, model);
    try {
      const first = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
      expect(first.status).toBe(200);
      expect(first.headers.get('cache-control')).toBe('no-store');
      expect(await first.json()).toEqual(minted);
      const second = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
      expect(await second.json()).toEqual(minted);
      expect(requestGithubToken).toHaveBeenCalledTimes(1);
      // Without the per-boot key nothing is served; a POST is not this route.
      expect((await fetch(`${adapter.baseUrl}/github/token`)).status).toBe(401);
      expect((await fetch(`${adapter.baseUrl}/github/token`, { method: 'POST', headers: { 'x-api-key': adapter.secret } })).status).not.toBe(200);
    } finally { await adapter.close(); }
  });

  it('mints again when the served token nears its horizon', async () => {
    const nearly = { ...minted, expires_at: Math.floor(Date.now() / 1000) + 60 };
    const requestGithubToken = jest.fn().mockResolvedValueOnce(nearly).mockResolvedValueOnce(minted);
    const adapter = await startCrewModelAdapter({ invokeModel: jest.fn(), requestGithubToken } as unknown as ChatDataClient, model);
    try {
      expect(await (await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } })).json()).toEqual(nearly);
      expect(await (await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } })).json()).toEqual(minted);
      expect(requestGithubToken).toHaveBeenCalledTimes(2);
    } finally { await adapter.close(); }
  });

  it.each([
    [404, 'GitHub not connected'],
    [403, 'GitHub consent required'],
    [409, 'GitHub reconnect required'],
    [503, 'not enabled on this deployment'],
  ])('names the step behind a gateway %s refusal and keeps nothing cached', async (status, text) => {
    const lines: string[] = [];
    const requestGithubToken = jest.fn().mockRejectedValueOnce(new ChatDataError(status === 409 ? 'scope_mismatch' : status >= 500 ? 'unavailable' : 'denied', status))
      .mockResolvedValueOnce(minted);
    const adapter = await startCrewModelAdapter({ invokeModel: jest.fn(), requestGithubToken } as unknown as ChatDataClient, model,
      undefined, true, HOST_MODEL_REQUEST_LIMITS, line => lines.push(line));
    try {
      const refused = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
      expect(refused.status).toBe(status);
      const body = (await refused.json()) as { error: { message: string } };
      expect(body.error.message).toContain(text);
      expect(JSON.stringify(body)).not.toContain('gho_');
      expect(lines.map(line => JSON.parse(line))).toEqual([expect.objectContaining({ event: 'crew_model_adapter_failure', route: 'github', status, gateway_status: status })]);
      expect((await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } })).status).toBe(200);
    } finally { await adapter.close(); }
  });

  it('discards the held token on turn rebind and re-mints inside the ten-minute horizon', async () => {
    let clock = Date.now();
    const clockSpy = jest.spyOn(Date, 'now').mockImplementation(() => clock);
    const firstMint = jest.fn().mockResolvedValue({ token: 'gho_first_owner', login: 'first', expires_at: Math.floor(clock / 1000) + 3600 });
    const secondMint = jest.fn().mockRejectedValueOnce(new ChatDataError('denied', 403))
      .mockImplementation(() => Promise.resolve({ token: 'gho_second_owner', login: 'second', expires_at: Math.floor(clock / 1000) + 3600 }));
    const first = { invokeModel: jest.fn(), requestGithubToken: firstMint } as unknown as ChatDataClient;
    const second = { invokeModel: jest.fn(), requestGithubToken: secondMint } as unknown as ChatDataClient;
    second.withSignal = jest.fn().mockReturnValue(second);
    const logs: string[] = [];
    const adapter = await startCrewModelAdapter(first, model, undefined, true, HOST_MODEL_REQUEST_LIMITS, line => logs.push(line));
    const url = `${adapter.baseUrl}/github/token`;
    const options = { headers: { 'x-api-key': adapter.secret } };
    try {
      const original = await (await fetch(url, options)).json() as { token: string; expires_at: number };
      expect(original.token).toBe('gho_first_owner');
      expect(original.expires_at).toBe(Math.floor(clock / 1000) + GITHUB_TOKEN_HORIZON_SECONDS);
      clock += 1000;
      adapter.bindTurn(second, model, new AbortController().signal);
      const refused = await fetch(url, options);
      expect(refused.status).toBe(403);
      expect(JSON.stringify(await refused.json())).not.toContain('gho_first_owner');
      expect(secondMint).toHaveBeenCalledTimes(1);
      const renewed = await (await fetch(url, options)).json() as { token: string; expires_at: number };
      expect(renewed.token).toBe('gho_second_owner');
      expect(renewed.expires_at).toBe(Math.floor(clock / 1000) + GITHUB_TOKEN_HORIZON_SECONDS);
      expect(secondMint).toHaveBeenCalledTimes(2);
      expect(await (await fetch(url, options)).json()).toEqual(renewed);
      expect(secondMint).toHaveBeenCalledTimes(2);
      clock += 301_000;
      secondMint.mockRejectedValueOnce(new ChatDataError('denied', 403));
      const stale = await fetch(url, options);
      expect(stale.status).toBe(403);
      expect(JSON.stringify(await stale.json())).not.toContain('gho_second_owner');
      expect(secondMint).toHaveBeenCalledTimes(3);
      expect(logs).toHaveLength(2);
      expect(logs.join('')).not.toMatch(/gho_first_owner|gho_second_owner/);
    } finally { await adapter.close(); clockSpy.mockRestore(); }
  });

  it('answers 503 when the client has no GitHub mint (a sandbox client), without crashing the adapter', async () => {
    const adapter = await startCrewModelAdapter({ invokeModel: jest.fn() } as unknown as ChatDataClient, model);
    try {
      const response = await fetch(`${adapter.baseUrl}/github/token`, { headers: { 'x-api-key': adapter.secret } });
      expect(response.status).toBe(503);
      expect(((await response.json()) as { error: { message: string } }).error.message).toContain('unavailable on this host');
    } finally { await adapter.close(); }
  });
});


describe('Claude Code non-default model request shape (dev 2026-10-25)', () => {
  const model = 'us.anthropic.example-v1:0';
  const invokeModel = jest.fn().mockResolvedValue({ modelId: model, stopReason: 'end_turn',
    content: [{ type: 'text', text: 'pong' }], usage: { input_tokens: 1, output_tokens: 1 } });
  beforeEach(() => invokeModel.mockClear());
  const started = () => startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
  const post = (adapter: Awaited<ReturnType<typeof started>>, body: Record<string, unknown>) =>
    fetch(`${adapter.baseUrl}/v1/messages`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
      body: JSON.stringify({ stream: false, thinking: { type: 'adaptive', display: 'updates' }, output_config: { effort: 'medium' },
        messages: [{ role: 'user', content: 'ping' }, { role: 'system', content: 'reminder', output_config: { effort: 'medium' } },
          { role: 'user', content: 'pong?', output_config: { effort: 'medium' } }], ...body }) });

  it('accepts message-level output_config and a 128000 max_tokens for the box model, forwarding neither as sent', async () => {
    // Claude Code sends `output_config: { effort }` on the last user message and max_tokens 128000 for Opus;
    // the strict message schema answered 400 "messages.1:unrecognized_keys, max_tokens:too_big".
    const adapter = await started();
    try {
      const response = await post(adapter, { model, max_tokens: 128_000 });
      expect(response.status).toBe(200);
      const forwarded = invokeModel.mock.calls[0][1] as { max_tokens: number; messages: Array<Record<string, unknown>> };
      expect(forwarded.max_tokens).toBe(HOST_MODEL_REQUEST_LIMITS.maxTokens);
      expect(forwarded.messages.map(message => Object.keys(message).sort())).toEqual([['content', 'role'], ['content', 'role'], ['content', 'role']]);
      expect(forwarded).not.toHaveProperty('output_config');
    } finally { await adapter.close(); }
  });

  it('forwards the model the owner chose on a Dev Box host and keeps the sandbox pinned', async () => {
    // Host profile: `/model opus` names another gateway model; the adapter passes it through as the trailing
    // model argument and accepts the reply under that id. The gateway applies the owner's entitlement.
    invokeModel.mockResolvedValueOnce({ modelId: 'global.anthropic.claude-opus-5-5', stopReason: 'end_turn',
      content: [{ type: 'text', text: 'pong' }], usage: { input_tokens: 1, output_tokens: 1 } });
    const adapter = await started();
    try {
      const response = await post(adapter, { model: 'global.anthropic.claude-opus-5-5', max_tokens: 128_000 });
      expect(response.status).toBe(200);
      expect(((await response.json()) as { model: string }).model).toBe('global.anthropic.claude-opus-5-5');
      expect(invokeModel.mock.calls[0][5]).toBe('global.anthropic.claude-opus-5-5');
      expect((await post(adapter, { model: 'not a model id', max_tokens: 10 })).status).toBe(400);
    } finally { await adapter.close(); }
    // Sandbox profile (chat): still exactly the pinned model.
    const sandbox = await startCrewModelAdapter({ invokeModel } as unknown as ChatDataClient, model);
    try {
      const refused = await fetch(`${sandbox.baseUrl}/v1/messages`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': sandbox.secret },
        body: JSON.stringify({ model: 'global.anthropic.claude-opus-5-5', max_tokens: 10, messages: [{ role: 'user', content: 'ping' }] }) });
      expect(refused.status).toBe(400);
      expect(((await refused.json()) as { error: { message: string } }).error.message).toContain('this sandbox serves');
    } finally { await sandbox.close(); }
  });
});

describe('Codex on GPT models: Responses passthrough (dev 2026-10-26)', () => {
  const model = 'us.anthropic.example-v1:0';
  const upstream = (status: number, contentType: string, chunks: string[]) => ({
    status, contentType, finish: jest.fn(async () => undefined), stopped: new AbortController(),
    body: new ReadableStream<Uint8Array>({ start(controller) { for (const c of chunks) controller.enqueue(new TextEncoder().encode(c)); controller.close(); } }),
  });

  it('forwards an openai.* Responses request untouched and relays the JSON reply bytes', async () => {
    const reply = upstream(200, 'application/json', ['{"output_text":"pong","model":"us.openai.gpt-6-astra"}']);
    const requestOpenAIResponses = jest.fn().mockResolvedValue(reply);
    const invokeModel = jest.fn();
    const adapter = await startCrewModelAdapter({ invokeModel, requestOpenAIResponses } as unknown as ChatDataClient, model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    try {
      const body = { model: 'openai.gpt-6-astra', input: 'reply pong', stream: false, reasoning: { effort: 'low' } };
      const response = await fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret }, body: JSON.stringify(body) });
      expect(response.status).toBe(200);
      expect(await response.text()).toBe('{"output_text":"pong","model":"us.openai.gpt-6-astra"}');
      expect(requestOpenAIResponses).toHaveBeenCalledWith(body, expect.any(AbortSignal));
      expect(invokeModel).not.toHaveBeenCalled();
      expect(reply.finish).toHaveBeenCalledWith(true);
    } finally { await adapter.close(); }
  });

  it('relays an SSE reply as it streams and keeps Anthropic models on the translated path', async () => {
    const reply = upstream(200, 'text/event-stream', ['event: response.created\ndata: {"type":"response.created"}\n\n', 'event: response.completed\ndata: {"type":"response.completed"}\n\n']);
    const requestOpenAIResponses = jest.fn().mockResolvedValue(reply);
    const invokeModel = jest.fn().mockResolvedValue({ modelId: model, stopReason: 'end_turn', content: [{ type: 'text', text: 'pong' }], usage: { input_tokens: 1, output_tokens: 1 } });
    const adapter = await startCrewModelAdapter({ invokeModel, requestOpenAIResponses } as unknown as ChatDataClient, model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    try {
      const streamed = await fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
        body: JSON.stringify({ model: 'us.openai.gpt-6-sol', input: 'reply pong', stream: true }) });
      expect(streamed.headers.get('content-type')).toBe('text/event-stream');
      expect(await streamed.text()).toContain('response.completed');
      const translated = await fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
        body: JSON.stringify({ model, input: [{ type: 'message', role: 'user', content: 'reply pong' }], stream: false }) });
      expect(translated.status).toBe(200);
      expect(invokeModel).toHaveBeenCalledTimes(1);
      expect(requestOpenAIResponses).toHaveBeenCalledTimes(1);
    } finally { await adapter.close(); }
  });

  it('passes the gateway refusal through as the Responses error when the owner is not entitled', async () => {
    const requestOpenAIResponses = jest.fn().mockRejectedValue(new ChatDataError('denied', 403));
    const adapter = await startCrewModelAdapter({ invokeModel: jest.fn(), requestOpenAIResponses } as unknown as ChatDataClient, model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    try {
      const response = await fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
        body: JSON.stringify({ model: 'openai.gpt-6-luna', input: 'x' }) });
      expect(response.status).toBe(400);
      expect(((await response.json()) as { error: { message: string } }).error.message).toContain('denied');
    } finally { await adapter.close(); }
  });

  it.each(['read', 'backpressure'])('finalizes once when the client disconnects during %s', async (waiting) => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const cancel = jest.fn();
    const reply = { ...upstream(200, 'text/event-stream', []),
      body: new ReadableStream<Uint8Array>({ start(value) { controller = value; }, cancel }) };
    let forwardedSignal: AbortSignal | undefined;
    const requestOpenAIResponses = jest.fn(async (_body: unknown, signal?: AbortSignal) => {
      forwardedSignal = signal;
      return reply;
    });
    const originalWrite = ServerResponse.prototype.write;
    const write = waiting === 'backpressure' ? jest.spyOn(ServerResponse.prototype, 'write')
      .mockImplementation(function (this: ServerResponse, chunk: Uint8Array) {
        originalWrite.call(this, chunk, 'utf8');
        return false;
      }) : undefined;
    const adapter = await startCrewModelAdapter({ requestOpenAIResponses } as unknown as ChatDataClient, model,
      undefined, true, HOST_MODEL_REQUEST_LIMITS, () => undefined);
    const aborted = new AbortController();
    try {
      controller.enqueue(new TextEncoder().encode('data: first\n\n'));
      const response = await fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', signal: aborted.signal,
        headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
        body: JSON.stringify({ model: 'openai.gpt-6-astra', input: 'reply', stream: true }) });
      expect((await response.body!.getReader().read()).done).toBe(false);
      aborted.abort();
      for (let attempt = 0; attempt < 200 && reply.finish.mock.calls.length === 0; attempt++)
        await new Promise(resolve => setTimeout(resolve, 2));
      expect(forwardedSignal?.aborted).toBe(true);
      expect(reply.stopped.signal.aborted).toBe(true);
      expect(cancel).toHaveBeenCalledTimes(1);
      expect(reply.finish).toHaveBeenCalledTimes(1);
      expect(reply.finish).toHaveBeenCalledWith(false);
    } finally {
      aborted.abort();
      write?.mockRestore();
      if (!cancel.mock.calls.length) controller.close();
      await adapter.close();
    }
  });

  it('cancels before upstream headers and finalizes a late upstream reply', async () => {
    const reply = upstream(200, 'text/event-stream', ['data: never delivered\n\n']);
    let resolveReply!: (value: typeof reply) => void;
    let forwardedSignal: AbortSignal | undefined;
    const requestOpenAIResponses = jest.fn((_body: unknown, signal?: AbortSignal) => {
      forwardedSignal = signal;
      return new Promise<typeof reply>(resolve => { resolveReply = resolve; });
    });
    const adapter = await startCrewModelAdapter({ requestOpenAIResponses } as unknown as ChatDataClient, model,
      undefined, true, HOST_MODEL_REQUEST_LIMITS, () => undefined);
    const aborted = new AbortController();
    const response = fetch(`${adapter.baseUrl}/v1/responses`, { method: 'POST', signal: aborted.signal,
      headers: { 'content-type': 'application/json', 'x-api-key': adapter.secret },
      body: JSON.stringify({ model: 'openai.gpt-6-astra', input: 'reply', stream: true }) }).catch(error => error);
    try {
      for (let attempt = 0; attempt < 200 && !forwardedSignal; attempt++) await new Promise(resolve => setTimeout(resolve, 2));
      expect(forwardedSignal).toBeDefined();
      aborted.abort();
      for (let attempt = 0; attempt < 200 && !forwardedSignal?.aborted; attempt++) await new Promise(resolve => setTimeout(resolve, 2));
      expect(forwardedSignal?.aborted).toBe(true);
      resolveReply(reply);
      for (let attempt = 0; attempt < 200 && reply.finish.mock.calls.length === 0; attempt++) await new Promise(resolve => setTimeout(resolve, 2));
      expect(reply.finish).toHaveBeenCalledTimes(1);
      expect(reply.finish).toHaveBeenCalledWith(false);
      expect(reply.stopped.signal.aborted).toBe(true);
    } finally {
      aborted.abort();
      resolveReply?.(reply);
      await response;
      await adapter.close();
    }
  });
});


describe('tools catalogue cache (#8835)', () => {
  // Kiro Crew's 15 s MCP probe and Claude Code's per-turn listing must not pay the host route's
  // 0.4–15.5 s every time (dev gen 51); the adapter answers GET /tools from the last good listing.
  const invokeModel = jest.fn();
  const requestTools = jest.fn();
  const lines: string[] = [];
  let adapter: Awaited<ReturnType<typeof startCrewModelAdapter>>;
  let now = 1_700_000_000_000;
  beforeEach(async () => {
    invokeModel.mockReset();
    requestTools.mockReset().mockResolvedValue({ status: 200, body: { tools: [{ name: 'websearch___search' }] } });
    lines.length = 0;
    jest.spyOn(Date, 'now').mockImplementation(() => now);
    adapter = await startCrewModelAdapter({ invokeModel, requestTools } as unknown as ChatDataClient, model, undefined, true,
      HOST_MODEL_REQUEST_LIMITS, line => lines.push(line));
  });
  afterEach(async () => { jest.restoreAllMocks(); await adapter.close(); });
  const list = (path = '/tools', init: RequestInit = {}) => fetch(`${adapter.baseUrl}${path}`, { ...init, headers: {
    'x-api-key': adapter.secret, ...(init.body ? { 'content-type': 'application/json' } : {}) } });
  const flush = () => new Promise(resolve => setImmediate(resolve));

  it('answers a repeated listing from memory and shares one upstream call between concurrent probes', async () => {
    const [first, second] = await Promise.all([list(), list()]);
    expect(first.status).toBe(200);
    expect(await second.json()).toEqual({ tools: [{ name: 'websearch___search' }] });
    expect(await (await list()).json()).toEqual({ tools: [{ name: 'websearch___search' }] });
    expect(requestTools).toHaveBeenCalledTimes(1);
  });

  it('refreshes in the background once the listing is older than the fresh window, serving the old one meanwhile', async () => {
    await list();
    requestTools.mockResolvedValue({ status: 200, body: { tools: [{ name: 'websearch___search' }, { name: 'adp___operations_list' }] } });
    now += TOOLS_LISTING_FRESH_MS;
    expect(await (await list()).json()).toEqual({ tools: [{ name: 'websearch___search' }] });
    await flush();
    expect(requestTools).toHaveBeenCalledTimes(2);
    expect((await (await list()).json() as { tools: unknown[] }).tools).toHaveLength(2);
    expect(requestTools).toHaveBeenCalledTimes(2);
  });

  it('lists again synchronously past the maximum age and never caches a refusal or a tool call', async () => {
    await list();
    now += TOOLS_LISTING_MAX_AGE_MS;
    requestTools.mockResolvedValueOnce({ status: 403, body: { error: 'denied' } });
    expect((await list()).status).toBe(403);
    expect((await list()).status).toBe(200);           // the refusal was not remembered; this call went upstream
    expect(requestTools).toHaveBeenCalledTimes(3);
    await list('/tools/websearch___search', { method: 'POST', body: JSON.stringify({ query: 'x' }) });
    await list('/tools/websearch___search', { method: 'POST', body: JSON.stringify({ query: 'x' }) });
    expect(requestTools).toHaveBeenCalledTimes(5);
    expect(requestTools).toHaveBeenLastCalledWith('POST', '/tools/websearch___search', { query: 'x' });
  });

  it('warms the catalogue for the current scope and forgets it when the scope ends', async () => {
    await expect(adapter.warmTools()).resolves.toBe(200);
    expect(requestTools).toHaveBeenCalledTimes(1);
    expect((await list()).status).toBe(200);
    expect(requestTools).toHaveBeenCalledTimes(1);
    adapter.suspend();
    adapter.bindTurn({ invokeModel, requestTools, withSignal() { return this; } } as unknown as ChatDataClient, model, new AbortController().signal);
    expect((await list()).status).toBe(200);
    expect(requestTools).toHaveBeenCalledTimes(2);
  });

  it('journals a failed background refresh and keeps serving the remembered listing', async () => {
    await list();
    now += TOOLS_LISTING_FRESH_MS;
    requestTools.mockRejectedValue(new ChatDataError('unavailable', 503));
    expect((await list()).status).toBe(200);
    await flush();
    expect(lines.map(line => JSON.parse(line))).toEqual([expect.objectContaining({ event: 'crew_model_adapter_failure', route: 'tools', gateway_code: 'unavailable' })]);
    expect((await list()).status).toBe(200);
  });
});

describe('Dev Box orchestration draft relay', () => {
  it('passes preview and inert registration to the bound host route', async () => {
    const requestOrchestration = jest.fn()
      .mockResolvedValueOnce({ status: 200, body: { plan_hash: 'fixture' } })
      .mockResolvedValueOnce({ status: 201, body: { flow_id: 'example-flow' } });
    const adapter = await startCrewModelAdapter({ requestOrchestration } as unknown as ChatDataClient,
      model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    const post = (path: string) => fetch(`${adapter.baseUrl}${path}`, {
      method: 'POST', headers: { authorization: `Bearer ${adapter.secret}`, 'content-type': 'application/json' },
      body: JSON.stringify({ flow_slug: 'example-flow' }),
    });
    try {
      expect((await post('/agent/orchestration/flows/drafts/preview')).status).toBe(200);
      const registered = await post('/agent/orchestration/flows/drafts');
      expect(registered.status).toBe(201);
      expect(await registered.json()).toEqual({ flow_id: 'example-flow' });
      expect(requestOrchestration.mock.calls).toEqual([
        ['POST', '/orchestration/flows/drafts/preview', { flow_slug: 'example-flow' }],
        ['POST', '/orchestration/flows/drafts', { flow_slug: 'example-flow' }],
      ]);
    } finally {
      await adapter.close();
    }
  });
  it('returns 404 for unlisted orchestration paths and never relays execution controls', async () => {
    const requestOrchestration = jest.fn();
    const adapter = await startCrewModelAdapter({ requestOrchestration } as unknown as ChatDataClient,
      model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    try {
      const denied = [
        ['GET', '/agent/orchestration/flows/flow-1/execution'],
        ['POST', '/agent/orchestration/flows/flow-1/execution'],
        ['POST', '/orchestration/flows/flow-1/execution'],
        ['POST', '/agent/orchestration/flows/flow-1/gates/approve'],
        ['DELETE', '/agent/orchestration/flows/drafts'],
        ['GET', '/agent/orchestration/flows/../execution'],
        ['GET', '/agent/orchestration/flows?limit=20&limit=21'],
        ['GET', '/agent/orchestration/flows?include_activity=true'],
        ['GET', '/agent/orchestration/flows/%2E%2E'],
      ];
      for (const [method, path] of denied) {
        const result = await fetch(`${adapter.baseUrl}${path}`, {
          method, headers: { authorization: `Bearer ${adapter.secret}` },
        });
        expect(result.status).toBe(404);
      }
      expect(requestOrchestration).not.toHaveBeenCalled();
    } finally {
      await adapter.close();
    }
  });

  it('keeps execution and approval off the relay during an owner read and draft scenario', async () => {
    const requestOrchestration = jest.fn().mockResolvedValue({ status: 200, body: { items: [] } });
    const adapter = await startCrewModelAdapter({ requestOrchestration } as unknown as ChatDataClient,
      model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    const authorized = { authorization: `Bearer ${adapter.secret}` };
    const draft = (path: string) => fetch(`${adapter.baseUrl}${path}`, {
      method: 'POST', headers: { ...authorized, 'content-type': 'application/json' },
      body: JSON.stringify({ flow_slug: 'example-flow' }),
    });
    try {
      const read = await fetch(`${adapter.baseUrl}/agent/orchestration/flows/flow-1`, { headers: authorized });
      expect(read.status).toBe(200);
      expect((await draft('/agent/orchestration/flows/drafts/preview')).status).toBe(200);
      const beforeDenials = requestOrchestration.mock.calls.length;
      for (const [method, path] of [
        ['GET', '/agent/orchestration/flows/flow-1/execution'],
        ['POST', '/agent/orchestration/flows/flow-1/execution'],
        ['POST', '/agent/orchestration/flows/flow-1/gates/approve'],
        ['GET', '/agent/orchestration/flows/flow-1/%65xecution'],
      ]) {
        const refused = await fetch(`${adapter.baseUrl}${path}`, { method, headers: authorized });
        expect(refused.status).toBe(404);
        expect(requestOrchestration).toHaveBeenCalledTimes(beforeDenials);
      }
      expect((await draft('/agent/orchestration/flows/drafts')).status).toBe(200);
      expect(requestOrchestration.mock.calls).toEqual([
        ['GET', '/orchestration/flows/flow-1', undefined],
        ['POST', '/orchestration/flows/drafts/preview', { flow_slug: 'example-flow' }],
        ['POST', '/orchestration/flows/drafts', { flow_slug: 'example-flow' }],
      ]);
    } finally {
      await adapter.close();
    }
  });

  it('relays only listed owner reads and percent-encoded draft reasons', async () => {
    const requestOrchestration = jest.fn().mockResolvedValue({ status: 200, body: { items: [] } });
    const adapter = await startCrewModelAdapter({ requestOrchestration } as unknown as ChatDataClient,
      model, undefined, true, HOST_MODEL_REQUEST_LIMITS);
    try {
      const paths = ['/products?limit=20&cursor=page%2Fnext', '/github/status',
        '/agent/orchestration/flows?needs_me=true', '/agent/orchestration/flows/flow-1',
        '/agent/orchestration/flows/flow-1/plans', '/agent/orchestration/flows/flow-1/decisions',
        '/products/p-1/projects/j-1/sources?resource_cursor=page%2Fnext'];
      for (const path of paths) {
        const result = await fetch(`${adapter.baseUrl}${path}`, { headers: { authorization: `Bearer ${adapter.secret}` } });
        expect(result.status).toBe(200);
      }
      const draft = await fetch(`${adapter.baseUrl}/agent/orchestration/flows/drafts?reason=review%3A+owner%26team`, {
        method: 'POST', headers: { authorization: `Bearer ${adapter.secret}`, 'content-type': 'application/json' },
        body: JSON.stringify({ flow_slug: 'draft' }),
      });
      expect(draft.status).toBe(200);
      expect(requestOrchestration.mock.calls.map(call => call[1])).toEqual([
        '/orchestration/products?limit=20&cursor=page%2Fnext', '/orchestration/github/status',
        '/orchestration/flows?needs_me=true', '/orchestration/flows/flow-1',
        '/orchestration/flows/flow-1/plans', '/orchestration/flows/flow-1/decisions',
        '/orchestration/products/p-1/projects/j-1/sources?resource_cursor=page%2Fnext',
        '/orchestration/flows/drafts?reason=review%3A+owner%26team',
      ]);
    } finally {
      await adapter.close();
    }
  });

});
