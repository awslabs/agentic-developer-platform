/** Pinned SDK MCP tool-loop qualification. Local fixture only; no credentials,
 * provider operations, live inference or production tool enablement. */
import { Codex } from '@openai/codex-sdk';
import { restrictedSdkConfig } from '../dist/sdk-config.js';
import { runSdkTurn } from '../dist/turn.js';
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = await mkdtemp(join(tmpdir(), 'adp-codex-tools-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(join(home, '.codex'), { recursive: true, mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const token = randomBytes(32).toString('hex');
const requests = [], calls = [], failures = [];
const server = http.createServer(async (request, response) => {
  try {
    if (request.method === 'GET') {
      response.writeHead(request.url === '/mcp' ? 405 : 426).end(); return;
    }
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.authorization, `Bearer ${token}`);
    let size = 0; const chunks = [];
    for await (const chunk of request) {
      size += chunk.length;
      assert.ok(size <= 65536); chunks.push(chunk);
    }
    const body = JSON.parse(Buffer.concat(chunks).toString());
    if (request.url === '/mcp') {
      if (body.id === undefined) { response.writeHead(202).end(); return; }
      let result;
      if (body.method === 'initialize') result = {
        protocolVersion: body.params.protocolVersion,
        capabilities: { tools: {} }, serverInfo: { name: 'adp-fixture', version: '1' },
      };
      else if (body.method === 'tools/list') result = { tools: [{
        name: 'read_evidence', description: 'Read a fixture evidence record.',
        inputSchema: { type: 'object', properties: { key: { type: 'string' } }, required: ['key'], additionalProperties: false },
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
      }] };
      else if (body.method === 'tools/call') {
        assert.equal(body.params.name, 'read_evidence');
        assert.deepEqual(body.params.arguments, { key: 'story-acceptance' });
        calls.push(body.params);
        assert.equal(calls.length, 1, 'Tool was replayed');
        result = { content: [{ type: 'text', text: 'evidence-receipt-471: requirement verified' }], isError: false };
      } else if (body.method === 'ping') result = {};
      else { response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ jsonrpc: '2.0', id: body.id, error: { code: -32601, message: 'Unsupported fixture method' } })); return; }
      response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ jsonrpc: '2.0', id: body.id, result })); return;
    }
    assert.equal(request.url, '/v1/responses');
    requests.push(body);
    assert.ok(requests.length <= 2, 'Unexpected model replay');
    const namespace = body.tools.find(tool => tool.type === 'namespace' && tool.name === 'mcp__adp');
    const tool = namespace?.tools.find(tool => tool.name === 'read_evidence');
    assert.ok(tool, `Missing MCP tool: ${JSON.stringify(body.tools)}`);
    let item;
    if (requests.length === 1) {
      item = { id: 'fc_fixture', type: 'function_call', call_id: 'call_fixture', name: tool.name, namespace: namespace.name,
        arguments: JSON.stringify({ key: 'story-acceptance' }), status: 'completed' };
    } else {
      const call = body.input.find(item => item.type === 'function_call');
      const output = body.input.find(item => item.type === 'function_call_output');
      assert.equal(call.call_id, 'call_fixture');
      assert.equal(output.call_id, 'call_fixture');
      assert.match(JSON.stringify(output.output), /evidence-receipt-471/);
      assert.equal(calls.length, 1);
      item = { id: 'msg_fixture', type: 'message', role: 'assistant', status: 'completed',
        content: [{ type: 'output_text', text: 'Fixture evidence verified.', annotations: [] }] };
    }
    const id = `resp_fixture_${requests.length}`;
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    const emit = (type, fields) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
    emit('response.created', { response: { id, object: 'response', status: 'in_progress', output: [] } });
    emit('response.output_item.added', { output_index: 0, item: { ...item, status: 'in_progress' } });
    if (item.type === 'function_call') {
      emit('response.function_call_arguments.done', { item_id: item.id, output_index: 0, arguments: item.arguments });
    } else {
      emit('response.output_text.delta', { item_id: item.id, output_index: 0, content_index: 0, delta: item.content[0].text });
    }
    emit('response.output_item.done', { output_index: 0, item });
    emit('response.completed', { response: { id, object: 'response', status: 'completed', output: [item],
      usage: { input_tokens: 100, output_tokens: 12, total_tokens: 112 } } });
    response.end();
  } catch (error) { failures.push(error); response.writeHead(400).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const origin = `http://127.0.0.1:${server.address().port}`;
  const codex = new Codex({ config: { ...restrictedSdkConfig(), mcp_servers: { adp: {
    url: `${origin}/mcp`, bearer_token_env_var: 'ADP_FIXTURE_MCP_TOKEN',
    required: true, enabled_tools: ['read_evidence'], startup_timeout_sec: 5, tool_timeout_sec: 5,
  } } }, baseUrl: `${origin}/v1`, apiKey: token,
    env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'),
      ADP_FIXTURE_MCP_TOKEN: token, XDG_CONFIG_HOME: join(home, '.config'),
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
  assert.deepEqual(failures, []);
  assert.equal(requests.length, 2);
  assert.equal(calls.length, 1);
  assert.equal(result.response, 'Fixture evidence verified.');
  console.log('Pinned SDK: authenticated host MCP discovery, single tool execution and correlated model continuation passed (fixture inference).');
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await rm(root, { recursive: true, force: true });
  if (failures.length) console.error(failures);
}
