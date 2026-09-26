import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { responsesRequest, responsesResult, responsesEvents, startResponsesProxy } from '../responses-proxy.mjs';
import { codexEnvironment, runCodexTask, startToolServer } from '../codex-runner.mjs';

const allowedTools = ['mcp__repository__read_file', 'mcp__repository__submit_patch'];
const tool = { type: 'function', name: allowedTools[0], parameters: { type: 'object', properties: {} } };
const options = { maxTokens: 512, allowedTools };
const request = { input: 'Inspect supplied code', tools: [tool], store: false };
const result = { turn_id: randomUUID(), stop_reason: 'end_turn', content: [{ type: 'text', text: 'Done' }], usage: { input_tokens: 2, output_tokens: 1 } };

test('Responses continuation preserves exact call IDs and removes built-in tool authority', () => {
  const body = { ...request, input: [{ role: 'developer', content: 'Policy' }, { role: 'user', content: 'Read' },
    { type: 'function_call', name: allowedTools[0], call_id: 'call_1', arguments: '{"path":"a.txt"}' },
    { type: 'function_call_output', call_id: 'call_1', output: 'Contents' }],
    tools: [tool, { type: 'custom', name: 'apply_patch' }, { type: 'web_search' }], max_output_tokens: 10000 };
  const normalized = responsesRequest(body, options);
  assert.equal(normalized.max_tokens, 512);
  assert.deepEqual(normalized.sdk_request.tools.map(tool => tool.name), [allowedTools[0]]);
  assert.equal(normalized.sdk_request.messages[1].content[0].id, 'call_1');
  assert.equal(normalized.sdk_request.messages[2].content[0].tool_use_id, 'call_1');
  assert.equal(normalized.sdk_request.system[0].text, 'Policy');
});

test('unbound history, unknown functions and modality state fail closed', () => {
  for (const body of [{ ...request, previous_response_id: 'resp_old' }, { ...request, store: true },
    { ...request, input: [{ type: 'reasoning', summary: [] }] },
    { ...request, input: [{ type: 'function_call_output', call_id: 'missing', output: 'bad' }] },
    { ...request, input: [{ type: 'function_call', call_id: 'call_1', name: 'shell', arguments: '{}' }] }]) {
    assert.throws(() => responsesRequest(body, options));
  }
  assert.throws(() => responsesResult({ ...result, content: [{ type: 'tool_use', id: 'call_1', name: 'shell', input: {} }] }, allowedTools));
});

test('SSE completion is correlated and truncation remains incomplete', () => {
  const response = responsesResult(result, allowedTools);
  const events = responsesEvents(response).split('\n\n').filter(Boolean).map(event => JSON.parse(event.split('\ndata: ')[1]));
  assert.equal(events.at(-1).type, 'response.completed');
  assert.equal(events.at(-1).response.id, 'resp_' + result.turn_id);
  assert.deepEqual(events.map(event => event.sequence_number), events.map((_, index) => index));
  assert.equal(response.usage.total_tokens, 3);
  assert.equal(responsesResult({ ...result, stop_reason: 'max_tokens' }, allowedTools).status, 'incomplete');
  assert.equal(responsesResult({ ...result, usage: undefined }, allowedTools).usage, null);
});

test('loopback model endpoint checks token and stops rather than retrying uncertain host delivery', async () => {
  let calls = 0, failure;
  const bridge = { model: async () => { calls++; throw new Error('unknown'); }, fail: error => { failure = error; } };
  const proxy = await startResponsesProxy(bridge, { ...options, maxRequests: 1 });
  try {
    assert.equal((await fetch(proxy.url + '/responses', { method: 'POST', body: JSON.stringify(request) })).status, 401);
    assert.equal(calls, 0);
    assert.equal((await fetch(proxy.url + '/responses', { method: 'POST', headers: { authorization: 'Bearer ' + proxy.token }, body: JSON.stringify(request) })).status, 502);
    assert.equal(calls, 1); assert.ok(failure);
  } finally { await proxy.close(); }
});

test('Codex environment never inherits real login/provider stores', () => {
  process.env.BG_CONFIG_DIR = '/live-login'; process.env.AWS_ACCESS_KEY_ID = 'do-not-inherit';
  process.env.OPENAI_API_KEY = 'do-not-inherit'; process.env.CODEX_HOME = '/live-codex';
  const env = codexEnvironment('/private', { token: 'loopback' }, { token: 'mcp-loopback' });
  assert.equal(env.CODEX_HOME, '/private/.codex'); assert.equal(env.BG_CONFIG_DIR, '/private/.adp');
  assert.equal(env.AWS_ACCESS_KEY_ID, undefined); assert.equal(env.OPENAI_API_KEY, undefined);
});

test('MCP exposes only the explicit registered repository operations', async () => {
  let called = 0;
  const server = await startToolServer([{ name: 'read_file', description: 'Read', inputSchema: { type: 'object' }, execute: async () => { called++; return { content: [{ type: 'text', text: 'ok' }] }; } }], { controller: new AbortController() });
  try {
    const call = async (method, params) => (await fetch(server.url, { method: 'POST', headers: { authorization: 'Bearer ' + server.token }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) })).json();
    assert.equal((await call('tools/list')).result.tools.length, 1);
    assert.ok((await call('tools/call', { name: 'shell' })).error);
    assert.equal(called, 0);
    assert.equal((await call('tools/call', { name: 'read_file', arguments: {} })).result.content[0].text, 'ok');
    assert.equal(called, 1);
  } finally { await server.close(); }
});

test('actual Codex CLI uses fake loopback model and approved MCP without provider credentials', { skip: !process.env.CODEX_BRIDGE_TEST_BIN, timeout: 45000 }, async () => {
  let calls = 0;
  const bridge = { controller: new AbortController(), report: null, failure: null,
    fail(error) { this.failure = error; this.controller.abort(); },
    async model(request, maxTokens) {
      calls++; assert.ok(maxTokens <= 512);
      assert.ok(request.tools.every(tool => tool.name.startsWith('mcp__repository__')));
      return { ...result, turn_id: randomUUID(), stop_reason: calls === 1 ? 'tool_use' : 'end_turn',
        content: calls === 1 ? [{ type: 'tool_use', id: 'call_submit', name: 'mcp__repository__submit_patch', input: { summary: 'Done' } }] : [{ type: 'text', text: 'Done' }] };
    } };
  const definitions = [{ name: 'submit_patch', description: 'Submit patch', inputSchema: { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'], additionalProperties: false },
    async execute(input) { bridge.report = { summary: input.summary }; return { content: [{ type: 'text', text: 'accepted' }] }; } }];
  const report = await runCodexTask({ instructions: 'Submit the completed patch using the repository MCP tool.', limits: { max_output_tokens_per_turn: 512, max_turns: 3, deadline_at: new Date(Date.now() + 30000).toISOString() } }, bridge, definitions, { binary: process.env.CODEX_BRIDGE_TEST_BIN });
  assert.equal(report.summary, 'Done'); assert.ok(calls >= 1 && calls <= 3);
});

test('same Responses payload retry shares the same host model turn and response identity', async () => {
  let calls = 0;
  const bridge = { model: async () => { calls++; await new Promise(resolve => setTimeout(resolve, 15)); return result; }, fail: error => { throw error; } };
  const proxy = await startResponsesProxy(bridge, { ...options, maxRequests: 1 });
  try {
    const call = () => fetch(proxy.url + '/responses', { method: 'POST', headers: { authorization: 'Bearer ' + proxy.token }, body: JSON.stringify(request) }).then(reply => reply.json());
    const [first, second] = await Promise.all([call(), call()]);
    assert.equal(calls, 1); assert.equal(first.id, second.id);
    assert.equal((await call()).id, first.id); assert.equal(calls, 1);
  } finally { await proxy.close(); }
});

test('Responses namespace calls round trip without widening the function allowlist', () => {
  const normalized = responsesRequest({ input: 'Read', tools: [{ type: 'namespace', name: 'mcp__repository', tools: [{ ...tool, name: 'read_file' }] }] }, options);
  assert.deepEqual(normalized.toolNames, [allowedTools[0]]);
  const output = responsesResult({ ...result, stop_reason: 'tool_use', content: [{ type: 'tool_use', id: 'call_1', name: allowedTools[0], input: {} }] }, normalized.toolNames, normalized.toolMap);
  assert.equal(output.output[0].name, 'read_file'); assert.equal(output.output[0].namespace, 'mcp__repository');
  const next = responsesRequest({ input: [{ role: 'user', content: 'Read' }, output.output[0], { type: 'function_call_output', call_id: 'call_1', output: [{ type: 'input_text', text: 'contents' }] }], tools: [tool] }, options);
  assert.equal(next.sdk_request.messages[1].content[0].name, allowedTools[0]);
});
