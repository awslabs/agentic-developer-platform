/** Real pinned SDK against deterministic local SSE, not a live model test. */
import { Codex } from '@openai/codex-sdk';
import { restrictedSdkConfig } from '../dist/sdk-config.js';
import { runSdkTurn } from '../dist/turn.js';
import http from 'node:http';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const root = await mkdtemp(join(tmpdir(), 'adp-codex-turn-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(join(home, '.codex'), { recursive: true, mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const requests = [];
const failures = [];
const cancellation = new AbortController();
const server = http.createServer(async (request, response) => {
  try {
    if (request.method === 'GET') {
      response.writeHead(426, { 'content-type': 'application/json', 'content-length': '2' }).end('{}');
      return;
    }
    assert.equal(request.method, 'POST');
    assert.equal(request.url, '/v1/responses');
    let bytes = 0;
    const parts = [];
    for await (const chunk of request) {
      bytes += chunk.length;
      assert.ok(bytes <= 65536, 'SDK request exceeds current task frame bound');
      parts.push(chunk);
    }
    const body = JSON.parse(Buffer.concat(parts).toString());
    requests.push(body);
    assert.ok(requests.length <= 4, 'Unexpected retry or extra model call');
    assert.equal(body.model, 'gpt-5-codex');
    assert.equal(body.reasoning.effort, 'medium');
    if (requests.length === 4) {
      setTimeout(() => cancellation.abort(new Error('fixture cancellation')), 10);
      return;
    }
    const text = requests.length === 1 ? 'fixture first reply' : 'fixture resumed reply';
    const id = `resp_fixture_${requests.length}`;
    const item = { id: `msg_fixture_${requests.length}`, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] };
    response.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': id });
    const emit = (type, fields) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
    emit('response.created', { response: { id, object: 'response', status: 'in_progress', output: [] } });
    emit('response.output_item.added', { output_index: 0, item: { ...item, status: 'in_progress', content: [] } });
    emit('response.content_part.added', { item_id: item.id, output_index: 0, content_index: 0, part: { type: 'output_text', text: '', annotations: [] } });
    emit('response.output_text.delta', { item_id: item.id, output_index: 0, content_index: 0, delta: text });
    emit('response.output_text.done', { item_id: item.id, output_index: 0, content_index: 0, text });
    emit('response.content_part.done', { item_id: item.id, output_index: 0, content_index: 0, part: item.content[0] });
    emit('response.output_item.done', { output_index: 0, item });
    emit('response.completed', { response: { id, object: 'response', status: 'completed', output: [item], usage: { input_tokens: 100, input_tokens_details: { cached_tokens: 20 }, output_tokens: 12, output_tokens_details: { reasoning_tokens: 4 }, total_tokens: 112 } } });
    response.end();
  } catch (error) {
    failures.push(error);
    response.writeHead(400).end();
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const codex = new Codex({ config: restrictedSdkConfig(), baseUrl: `http://127.0.0.1:${server.address().port}/v1`, apiKey: 'fixture-only-invalid-key',
    env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'), XDG_CONFIG_HOME: join(home, '.config'), XDG_CACHE_HOME: join(home, '.cache'), TMPDIR: root, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' } });
  const options = { workingDirectory: workspace, skipGitRepoCheck: true, model: 'gpt-5-codex', sandboxMode: 'read-only', approvalPolicy: 'never', networkAccessEnabled: false, webSearchMode: 'disabled', modelReasoningEffort: 'medium' };
  const context = { runId: 'fixture', personaKey: 'fixture', model: options.model, harnessRevision: 'fixture', surface: 'task-api', timeoutMs: 15000, maxInputBytes: 1000, maxOutputBytes: 1000, signal: AbortSignal.timeout(30000) };
  const progress = [];
  const first = await runSdkTurn(codex.startThread(options), 'Fixture first question; reply without tools.', context, async event => { progress.push(event); });
  assert.equal(first.response, 'fixture first reply');
  assert.equal(first.usage.input_tokens, 100);
  assert.equal(first.usage.cached_input_tokens, 20);
  assert.equal(first.usage.output_tokens, 12);
  assert.equal(first.usage.reasoning_output_tokens, 4);
  const resumed = await runSdkTurn(codex.resumeThread(first.threadId, options), 'Fixture follow-up; reply without tools.', context, async event => { progress.push(event); });
  assert.equal(resumed.response, 'fixture resumed reply');
  assert.equal(resumed.threadId, first.threadId);
  assert.equal(requests.length, 2);
  assert.ok(JSON.stringify(requests[1].input).includes('fixture first reply'), 'Resume omitted previous assistant output');
  assert.ok(JSON.stringify(requests[1].input).includes('Fixture first question'), 'Resume omitted previous input');
  assert.equal(progress.filter(event => event.type === 'turn.started').length, 2);
  await assert.rejects(runSdkTurn(codex.startThread(options), 'Fixture output overflow.', { ...context, maxOutputBytes: 1 }, async () => {}), /output budget/);
  await assert.rejects(runSdkTurn(codex.startThread(options), 'Fixture pending cancellation.', { ...context, signal: cancellation.signal }, async () => {}));
  assert.equal(cancellation.signal.aborted, true);
  assert.equal(requests.length, 4);
  assert.deepEqual(failures, []);
  console.log('Pinned SDK: two successful streamed harness turns, usage, progress, disk resume, output overflow and active cancellation verified against local SSE fixture (no live model).');
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await rm(root, { recursive: true, force: true });
}
