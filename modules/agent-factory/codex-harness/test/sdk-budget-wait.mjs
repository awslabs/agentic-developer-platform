/** Pinned worker SDK through the production model proxy and a local, deterministic gateway. */
import { Codex } from '@openai/codex-sdk';
import { restrictedSdkConfig } from '../dist/sdk-config.js';
import { runSdkTurn } from '../dist/turn.js';
import { readFileSync } from 'node:fs';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import vm from 'node:vm';
import ts from 'typescript';
import assert from 'node:assert/strict';

const production = ts.transpileModule(readFileSync(new URL('../../agent/src/sigv4-proxy.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
// Exercise the production reporter without opening the proxy dependency allowlist.
const admissionProgress = {};
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../../agent/src/lib/modelAdmissionProgress.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: admissionProgress });
const root = await mkdtemp(join(tmpdir(), 'adp-codex-budget-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(join(home, '.codex'), { recursive: true, mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const requests = [];
const failures = [];
let mode = 'budget';
const retryAfterSeconds = process.env.ADP_BUDGET_WAIT_TEST_LONG === '1' ? 65 : 1;
let proxy;
const signed = [];
const upstream = http.createServer(async (request, response) => {
  try {
    if (request.method === 'GET') { response.writeHead(426).end('{}'); return; }
    assert.equal(request.method, 'POST');
    assert.equal(request.url, '/agent/openai/v1/responses');
    const parts = [];
    for await (const part of request) parts.push(part);
    const body = JSON.parse(Buffer.concat(parts).toString());
    requests.push(body);
    assert.ok(requests.length <= 4, 'Unexpected model replay');
    if (mode === 'stream_failure') {
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.write('event: response.created\ndata: {"type":"response.created","response":{"id":"resp_failure","status":"in_progress","output":[]}}\n\n');
      response.end('event: error\ndata: {"type":"error","code":"usage_settlement_unconfirmed","message":"Model usage could not be confirmed; do not retry this model call.","param":null,"sequence_number":1}\n\n');
      return;
    }
    if (mode === 'settlement' || mode === 'gateway_settlement') {
      response.writeHead(502, { 'content-type': 'application/json', 'x-amzn-errortype': 'InternalFailure' });
      const payload = mode === 'settlement'
        ? { error: 'usage_settlement_unconfirmed', retryable: false }
        : { detail: { error: 'usage_settlement_unconfirmed', details: { retryable: false } } };
      response.end(JSON.stringify(payload));
      return;
    }
    if (requests.length === 1) {
      response.writeHead(429, { 'content-type': 'application/json', 'retry-after': String(retryAfterSeconds) });
      response.end(JSON.stringify({ error: 'budget_reservations_pending', details: { scope: 'flow' } }));
      return;
    }
    const text = 'Budget hold cleared.';
    const id = 'resp_budget_fixture';
    const item = { id: 'msg_budget_fixture', type: 'message', role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text, annotations: [] }] };
    response.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': id });
    const emit = (type, fields) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
    emit('response.created', { response: { id, object: 'response', status: 'in_progress', output: [] } });
    emit('response.output_item.added', { output_index: 0, item: { ...item, status: 'in_progress', content: [] } });
    emit('response.content_part.added', { item_id: item.id, output_index: 0, content_index: 0, part: { type: 'output_text', text: '', annotations: [] } });
    emit('response.output_text.delta', { item_id: item.id, output_index: 0, content_index: 0, delta: text });
    emit('response.output_text.done', { item_id: item.id, output_index: 0, content_index: 0, text });
    emit('response.content_part.done', { item_id: item.id, output_index: 0, content_index: 0, part: item.content[0] });
    emit('response.output_item.done', { output_index: 0, item });
    emit('response.completed', { response: { id, object: 'response', status: 'completed', output: [item],
      usage: { input_tokens: 100, input_tokens_details: { cached_tokens: 0 }, output_tokens: 12,
        output_tokens_details: { reasoning_tokens: 0 }, total_tokens: 112 } } });
    response.end();
  } catch (error) {
    failures.push(error);
    response.writeHead(400).end('{}');
  }
});
await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
try {
  vm.runInNewContext(production, {
    exports: {}, Buffer,
    require: name => ({
      url: { URL },
      http: { ...http, createServer: handler => (proxy = http.createServer(handler)) },
      https: { request: (options, callback) => http.request({ ...options, hostname: '127.0.0.1', port: upstream.address().port }, callback) },
      '@smithy/signature-v4': { SignatureV4: class { async sign(value) { signed.push({ method: value.method, path: value.path }); return value; } } },
      '@smithy/hash-node': { Hash: function () {} },
      './lib/runIdentity': { gatewaySigningRegion: () => 'us-east-1' },
      './lib/knowledgeBridge': { handleKnowledgeBridge: async () => false },
      './lib/proxyPort': { proxyPort: () => '0' },
      './lib/responsesOutputBound': { responsesOutputDefault: () => 1, withResponsesOutputBound: (_path, body) => body },
      './lib/modelAdmissionProgress': admissionProgress,
    })[name] ?? (() => { throw new Error(`Unexpected proxy dependency: ${name}`); })(),
    process: { argv: ['node', 'proxy', '--target', 'https://test.invalid/agent', '--port', '0'], env: { ADP_BUDGET_WAIT_MAX_SECONDS: '4200' } },
    console: { log() {}, error(message) { failures.push(new Error(message)); } }, setTimeout, clearTimeout,
  });
  if (!proxy.listening) await new Promise(resolve => proxy.once('listening', resolve));
  assert.ok(proxy.timeout > 4_200_000 + 600_000, 'Socket must survive budget wait plus provider idle time');
  const port = proxy.address().port;
  const codex = new Codex({ config: restrictedSdkConfig(), baseUrl: `http://127.0.0.1:${port}/openai/v1`,
    apiKey: 'fixture-only-invalid-key',
    env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'),
      XDG_CONFIG_HOME: join(home, '.config'), XDG_CACHE_HOME: join(home, '.cache'), TMPDIR: root,
      GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' } });
  const options = { workingDirectory: workspace, skipGitRepoCheck: true, model: 'gpt-5-codex',
    sandboxMode: 'read-only', approvalPolicy: 'never', networkAccessEnabled: false, webSearchMode: 'disabled', modelReasoningEffort: 'medium' };
  const context = { runId: 'budget-fixture', personaKey: 'fixture', model: options.model, harnessRevision: 'fixture',
    surface: 'task-api', timeoutMs: retryAfterSeconds * 1000 + 19000, maxInputBytes: 1000, maxOutputBytes: 1000,
    signal: AbortSignal.timeout(retryAfterSeconds * 1000 + 24000) };
  const started = Date.now();
  const result = await runSdkTurn(codex.startThread(options), 'Respond without tools.', context, async () => {});
  assert.equal(result.response, 'Budget hold cleared.');
  assert.equal(result.usage.input_tokens, 100);
  assert.ok(Date.now() - started >= retryAfterSeconds * 1000 - 100, 'SDK completed before Retry-After elapsed');
  assert.equal(requests.length, 2, 'Only a denied pre-provider call and one admitted model call');
  assert.equal(signed.filter(value => value.method === 'POST' && value.path === '/agent/openai/v1/responses').length, 2, 'Each model attempt is signed separately');
  assert.deepEqual(failures, []);
  for (const failureMode of ['settlement', 'gateway_settlement']) {
    mode = failureMode;
    requests.length = 0;
    await assert.rejects(runSdkTurn(codex.startThread(options), 'Reply without tools.', context, async () => {}));
    assert.equal(requests.length, 1, `SDK must not repeat a provider operation after ${failureMode}`);
  }
  mode = 'stream_failure';
  requests.length = 0;
  await assert.rejects(runSdkTurn(codex.startThread(options), 'Reply without tools.', context, async () => {}));
  assert.equal(requests.length, 1, 'SDK must not repeat a streaming provider operation after accounting fails');
  console.log('Pinned worker SDK: budget denial waited before one admitted model completion; accounting failure not replayed (no live model).');
} finally {
  proxy?.closeAllConnections(); upstream.closeAllConnections();
  await Promise.all([proxy, upstream].filter(Boolean).map(server => new Promise(resolve => server.close(resolve))));
  await rm(root, { recursive: true, force: true });
}
