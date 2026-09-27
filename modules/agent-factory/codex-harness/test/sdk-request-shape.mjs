/** Probe the actual pinned SDK transport against a local rejecting fixture.
 * No model invocation, provider credential, tool execution or external endpoint.
 */
import { Codex } from '@openai/codex-sdk';
import { restrictedSdkConfig } from '../dist/sdk-config.js';
import http from 'node:http';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = await mkdtemp(join(tmpdir(), 'adp-codex-shape-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(home, { mode: 0o700 });
await mkdir(join(home, '.codex'), { mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const baseline = process.argv.includes('--baseline');
let shape;
const server = http.createServer(async (request, response) => {
  const parts = [];
  let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > 1024 * 1024) { response.writeHead(413).end(); return; }
    parts.push(chunk);
  }
  if (request.method === 'POST' && request.url?.endsWith('/responses')) {
    const body = JSON.parse(Buffer.concat(parts).toString());
    shape = {
      sdk: '0.155.1', profile: baseline ? 'sdk-default' : 'adp-restricted', model: body.model, method: request.method, path: request.url, bytes,
      fields: Object.keys(body).sort(), stream: body.stream,
      inputItems: Array.isArray(body.input) ? body.input.length : 0,
      tools: (body.tools ?? []).map(tool => ({ type: tool.type, name: tool.name })),
      fitsCurrentTaskFrame: bytes <= 65536,
    };
  }
  // A denial prevents any model output or tool execution; it is intentional.
  response.writeHead(403, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ error: { code: 'fixture_denial', message: 'Local transport fixture only' } }));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const codex = new Codex({ config: baseline ? {} : restrictedSdkConfig(), baseUrl: `http://127.0.0.1:${server.address().port}/v1`, apiKey: 'fixture-only-invalid-key',
    env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'),
      XDG_CONFIG_HOME: join(home, '.config'), XDG_CACHE_HOME: join(home, '.cache'),
      TMPDIR: root, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' } });
  const thread = codex.startThread({ workingDirectory: workspace, skipGitRepoCheck: true,
    model: 'gpt-5-codex', sandboxMode: 'read-only', approvalPolicy: 'never',
    networkAccessEnabled: false, webSearchMode: 'disabled', modelReasoningEffort: 'medium' });
  let failure;
  await assert.rejects(thread.run('Transport fixture: reply OK. Do not use any tools.', { signal: AbortSignal.timeout(15000) }), error => { failure = String(error); return true; });
  assert.ok(shape, `SDK did not reach the local Responses fixture: ${failure?.slice(0, 1500)}`);
  assert.equal(shape.stream, true);
  assert.equal(shape.model, 'gpt-5-codex');
  if (!baseline) {
    const names = shape.tools.map(tool => tool.name);
    for (const forbidden of ['exec_command', 'write_stdin', 'shell', 'shell_command', 'multi_agent_v1', 'spawn_agent', 'create_goal', 'update_goal', 'get_goal']) assert.ok(!names.includes(forbidden), `Forbidden native tool exposed: ${forbidden}`);
  }
  console.log(JSON.stringify(shape, null, 2));
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await rm(root, { recursive: true, force: true });
}
