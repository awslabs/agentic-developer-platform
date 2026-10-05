/** Actual SDK through the production text-only bridge; fixture host, no gateway. */
import { Codex } from '@openai/codex-sdk';
import { restrictedSdkConfig } from '../dist/sdk-config.js';
import { runSdkTurn } from '../dist/turn.js';
import { startTextResponsesProxy } from '../dist/responses-proxy.js';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const root = await mkdtemp(join(tmpdir(), 'adp-codex-proxy-'));
const home = join(root, 'home');
const workspace = join(root, 'workspace');
await mkdir(join(home, '.codex'), { recursive: true, mode: 0o700 });
await mkdir(workspace, { mode: 0o700 });
const requests = [];
const policy = { model: 'gpt-5-codex', effort: 'medium', maxOutputTokens: 1000, maxRequestBytes: 65536, maxResponseBytes: 65536, maxOperations: 2, timeoutMs: 10000 };
const proxy = await startTextResponsesProxy(async request => {
  requests.push(request);
  assert.equal(request.reasoning.effort, 'medium');
  assert.equal(request.max_output_tokens, 1000);
  for (const forbidden of ['model', 'tools', 'prompt_cache_key', 'client_metadata', 'store']) assert.equal(forbidden in request, false);
  return { operationStatus: 'confirmed', response: {
    id: `resp_fixture_${requests.length}`, status: 'completed',
    output: [{ id: `rs_fixture_${requests.length}`, type: 'reasoning', summary: [], encrypted_content: 'fixture-opaque-reasoning' }, { id: `msg_fixture_${requests.length}`, type: 'message', role: 'assistant', status: 'completed', phase: 'final_answer', content: [{ type: 'output_text', text: 'Fixture host confirmed.', annotations: [] }] }],
    usage: { input_tokens: 100, output_tokens: 12, input_tokens_details: { cached_tokens: 20 }, output_tokens_details: { reasoning_tokens: 4 } },
  } };
}, policy);
try {
  const codex = new Codex({ config: restrictedSdkConfig(), baseUrl: proxy.baseUrl, apiKey: proxy.token,
    env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, '.codex'), XDG_CONFIG_HOME: join(home, '.config'), XDG_CACHE_HOME: join(home, '.cache'), TMPDIR: root, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' } });
  const options = { workingDirectory: workspace, skipGitRepoCheck: true, model: policy.model, sandboxMode: 'read-only', approvalPolicy: 'never', networkAccessEnabled: false, webSearchMode: 'disabled', modelReasoningEffort: policy.effort };
  const context = { runId: 'fixture', personaKey: 'fixture', model: policy.model, harnessRevision: 'fixture', surface: 'task-api', timeoutMs: 15000, maxInputBytes: 1000, maxOutputBytes: 1000, signal: AbortSignal.timeout(30000) };
  const first = await runSdkTurn(codex.startThread(options), 'Fixture question.', context, async () => {});
  assert.equal(first.response, 'Fixture host confirmed.');
  assert.equal(first.usage.reasoning_output_tokens, 4);
  const second = await runSdkTurn(codex.resumeThread(first.threadId, options), 'Fixture follow-up.', context, async () => {});
  assert.equal(second.response, first.response);
  assert.equal(requests.length, 2);
  assert.ok(JSON.stringify(requests[1].input).includes(first.response));
  assert.ok(requests[1].input.some(item => item.type === 'reasoning' && item.encrypted_content === 'fixture-opaque-reasoning' && !('id' in item)));
  assert.ok(requests[1].input.some(item => item.phase === 'final_answer'));
  console.log('Actual pinned SDK -> text Responses bridge -> fixture host -> validated SSE -> shared turn consumer: two turns passed. No live Task API/model.');
} finally {
  await proxy.close();
  await rm(root, { recursive: true, force: true });
}
