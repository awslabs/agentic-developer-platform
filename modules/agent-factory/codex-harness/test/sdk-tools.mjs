/** Pinned SDK MCP tool-loop qualification. Local fixture only; no credentials,
 * provider operations, live inference or production tool enablement. */
import { z } from 'zod';
import { startTextResponsesProxy } from '../dist/responses-proxy.js';
import { startToolServer } from '../dist/tool-server.js';
import { Codex } from '@openai/codex-sdk';
import { restrictedSdkConfig } from '../dist/sdk-config.js';
import { runSdkTurn } from '../dist/turn.js';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = await mkdtemp(join(tmpdir(), 'adp-codex-tools-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(join(home, '.codex'), { recursive: true, mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const requests = [], calls = [];
let currentChecks = 0;
const definitions = [{
  name: 'read_evidence', description: 'Read a fixture evidence record.',
  capability: 'repository.read', readOnly: true,
  input: z.object({ key: z.string() }),
}];
const mcp = await startToolServer(definitions, {
  async assertCurrent() { currentChecks++; },
  async execute(name, args) {
    assert.equal(name, 'read_evidence');
    assert.deepEqual(args, { key: 'story-acceptance' });
    calls.push({ name, args });
    assert.equal(calls.length, 1, 'Tool was replayed');
    return { status: 'confirmed', content: 'evidence-receipt-471: requirement verified' };
  },
}, { capabilities: ['repository.read'], maxCalls: 2, maxRequestBytes: 65536,
  maxResultBytes: 8192, timeoutMs: 5000, signal: AbortSignal.timeout(20000) });
const proxy = await startTextResponsesProxy(async body => {
  requests.push(body);
  assert.ok(requests.length <= 2, 'Unexpected model replay');
  assert.equal(body.tools.length, 1, 'Native tools leaked into model request');
  assert.equal(body.parallel_tool_calls, false);
  const namespace = body.tools[0];
  assert.equal(namespace.name, 'mcp__adp');
  const tool = namespace.tools[0];
  assert.equal(tool.name, 'read_evidence');
  const item = requests.length === 1
    ? { id: 'fc_fixture', type: 'function_call', call_id: 'call_fixture', name: tool.name, namespace: namespace.name,
      arguments: JSON.stringify({ key: 'story-acceptance' }), status: 'completed' }
    : { id: 'msg_fixture', type: 'message', role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text: 'Fixture evidence verified.', annotations: [] }] };
  return { operationStatus: 'confirmed', response: { id: `resp_fixture_${requests.length}`, status: 'completed', output: [item],
    usage: { input_tokens: 100, output_tokens: 12 } } };
}, { model: 'gpt-5-codex', effort: 'medium', maxOutputTokens: 100,
  maxRequestBytes: 65536, maxResponseBytes: 8192, maxOperations: 2, timeoutMs: 15000,
  tools: { definitions, validateHistory(history) {
    if (!requests.length) { assert.deepEqual(history, []); return true; }
    assert.equal(history.length, 2);
    assert.equal(history[0].call_id, 'call_fixture');
    assert.equal(history[0].name, 'read_evidence');
    assert.deepEqual(JSON.parse(history[0].arguments), { key: 'story-acceptance' });
    assert.equal(history[1].call_id, 'call_fixture');
    assert.equal(calls.length, 1);
    assert.equal(history[1].output.length, 2);
    assert.equal(history[1].output[0].type, 'input_text');
    assert.match(history[1].output[0].text, /^Wall time: [0-9]+(?:\.[0-9]+)? seconds\nOutput:$/);
    assert.deepEqual(history[1].output[1], { type: 'input_text', text: 'evidence-receipt-471: requirement verified' });
    return true;
  } },
});
try {
  const codex = new Codex({ config: { ...restrictedSdkConfig(), mcp_servers: { adp: {
    url: mcp.url, bearer_token_env_var: 'ADP_FIXTURE_MCP_TOKEN',
    required: true, enabled_tools: ['read_evidence'], startup_timeout_sec: 5, tool_timeout_sec: 5,
  } } }, baseUrl: proxy.baseUrl, apiKey: proxy.token,
    env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'),
      ADP_FIXTURE_MCP_TOKEN: mcp.token, XDG_CONFIG_HOME: join(home, '.config'),
      XDG_CACHE_HOME: join(home, '.cache'), TMPDIR: root,
      GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' } });
  const options = { workingDirectory: workspace, skipGitRepoCheck: true, model: 'gpt-5-codex',
    sandboxMode: 'read-only', approvalPolicy: 'never', networkAccessEnabled: false,
    webSearchMode: 'disabled', modelReasoningEffort: 'medium' };
  const result = await runSdkTurn(codex.startThread(options), 'Use fixture evidence to verify acceptance.', {
    runId: 'fixture', personaKey: 'fixture', model: options.model, harnessRevision: 'fixture',
    surface: 'task-api', timeoutMs: 15000, maxInputBytes: 1000, maxOutputBytes: 4096,
    signal: AbortSignal.timeout(20000),
  }, async () => {});
  assert.equal(requests.length, 2);
  assert.equal(calls.length, 1);
  assert.ok(currentChecks >= 4, 'Missing current authority checks');
  assert.equal(result.response, 'Fixture evidence verified.');
  console.log('Pinned SDK: authenticated host MCP discovery, single tool execution and correlated model continuation passed (fixture inference).');
} finally {
  await mcp.close();
  await proxy.close();
  await rm(root, { recursive: true, force: true });
}
