import { query } from '@anthropic-ai/claude-agent-sdk';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import ts from 'typescript';

const production = ts.transpileModule(readFileSync(new URL('../src/sigv4-proxy.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
// Load the real diagnostic reporter while keeping the proxy dependency allowlist
// closed. Settlement refusal and ordinary provider retries still run unchanged.
const admissionProgress = {};
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../src/lib/modelAdmissionProgress.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: admissionProgress });
const root = await mkdtemp(join(tmpdir(), 'adp-claude-settlement-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(home, { mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const model = 'global.anthropic.claude-opus-4-6-v1';
const failures = [];
let proxy;
let modelRequests = 0;
let mode = 'settlement';
let controller;
const upstream = http.createServer(async (request, response) => {
  try {
    if (request.method === 'GET' && /^\/agent\/inference-profiles(?:[/?]|$)/.test(request.url)) {
      response.writeHead(200, { 'content-type': 'application/json' });
      const profile = { inferenceProfileId: model,
        models: [{ modelArn: 'arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-6-v1' }] };
      response.end(JSON.stringify(request.url.includes('?') ? { inferenceProfileSummaries: [profile] } : profile));
      return;
    }
    assert.equal(request.method, 'POST', `Unexpected fixture request: ${request.method} ${request.url}`);
    assert.match(request.url, /^\/agent\/model\/[^/]+\/invoke(?:-with-response-stream)?$/);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    assert.ok(Array.isArray(JSON.parse(Buffer.concat(chunks).toString()).messages));
    modelRequests += 1;
    response.writeHead(502, { 'content-type': 'application/json', 'x-amzn-errortype': 'InternalFailure', 'x-should-retry': 'true' });
    response.end(JSON.stringify({ detail: {
      error: mode === 'settlement' ? 'usage_settlement_unconfirmed' : 'provider_unavailable',
      details: { retryable: false },
    } }));
    if (modelRequests >= 2) controller.abort();
  } catch (error) {
    failures.push(error);
    response.writeHead(400).end('{}');
    controller?.abort();
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
      '@smithy/signature-v4': { SignatureV4: class { async sign(value) { return value; } } },
      '@smithy/hash-node': { Hash: function () {} },
      './lib/runIdentity': { gatewaySigningRegion: () => 'us-east-1' },
      './lib/knowledgeBridge': { handleKnowledgeBridge: async () => false },
      './lib/proxyPort': { proxyPort: () => '0' },
      './lib/responsesOutputBound': { responsesOutputDefault: () => 1, withResponsesOutputBound: (_path, body) => body },
      './lib/modelAdmissionProgress': admissionProgress,
    })[name] ?? (() => { throw new Error(`Unexpected proxy dependency: ${name}`); })(),
    process: { argv: ['node', 'proxy', '--target', 'https://test.invalid/agent', '--port', '0'], env: {} },
    console: { log() {}, error(message) { failures.push(new Error(message)); } }, setTimeout, clearTimeout,
  });
  if (!proxy.listening) await new Promise(resolve => proxy.once('listening', resolve));
  for (const failureMode of ['settlement', 'provider']) {
    mode = failureMode;
    modelRequests = 0;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    let result;
    let queryError;
    const session = query({ prompt: 'Reply without tools.', options: {
      cwd: workspace, model, tools: [], settingSources: [], persistSession: false,
      permissionMode: 'default', maxTurns: 1, abortController: controller,
      env: {
        PATH: process.env.PATH, HOME: home, CLAUDE_CONFIG_DIR: home, TMPDIR: root,
        XDG_CONFIG_HOME: home, XDG_CACHE_HOME: home,
        CLAUDE_CODE_USE_BEDROCK: '1', CLAUDE_CODE_SKIP_BEDROCK_AUTH: '1',
        ANTHROPIC_BEDROCK_BASE_URL: `http://127.0.0.1:${proxy.address().port}`,
        AWS_REGION: 'us-east-1', AWS_DEFAULT_REGION: 'us-east-1', AWS_EC2_METADATA_DISABLED: 'true',
        AWS_CONFIG_FILE: '/dev/null', AWS_SHARED_CREDENTIALS_FILE: '/dev/null',
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1',
        CLAUDE_CODE_DISABLE_BUNDLED_SKILLS: '1', CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS: '1',
      },
    } });
    try {
      for await (const message of session) {
        if (message.type === 'result') result = message;
      }
    } catch (error) {
      queryError = error;
    } finally {
      clearTimeout(timeout);
      session.close();
    }
    assert.deepEqual(failures, []);
    if (mode === 'settlement') {
      assert.equal(modelRequests, 1, 'Claude SDK must not replay a model operation after accounting fails');
      assert.equal(controller.signal.aborted, false, 'Refusal must terminate without test cancellation');
      assert.equal(result?.is_error, true, `Expected a terminal SDK error result, got ${queryError}`);
    } else {
      assert.ok(modelRequests >= 2, 'Ordinary provider errors must retain their existing retry behavior');
    }
  }
  console.log('Pinned Claude SDK: one model request on accounting refusal; ordinary provider retries preserved (local fixtures only).');
} finally {
  controller?.abort();
  proxy?.closeAllConnections(); upstream.closeAllConnections();
  await Promise.all([proxy, upstream].filter(Boolean).map(server => new Promise(resolve => server.close(resolve))));
  await rm(root, { recursive: true, force: true });
}
