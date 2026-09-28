/** Actual shared session, pinned SDK, Responses bridge and MCP transport.
 * Host model/tool receipts are deterministic fixtures; no external effects. */
import assert from 'node:assert/strict';
import { readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { z } from 'zod';
import { snapshotPersona } from '../dist/persona.js';
import { HARNESS_CONTRACT_REVISION } from '../dist/admission.js';
import { runAdmittedSession } from '../dist/session.js';

const toolError = process.argv.includes('--tool-error');
const completionMode = process.argv.find(value => value.startsWith('--completion-'))?.slice('--completion-'.length);
const modelLimit = completionMode === 'exhausted' ? 4 : completionMode ? 3 : 2;
let completionChecks = 0, revoked = false;
const capabilities = completionMode ? ['repository.read', 'repository.write', 'tests.run', 'change.create'] : ['repository.read'];
const toolText = toolError ? 'Evidence unavailable: fixture confirmed error.' : 'evidence-receipt-471: requirement verified';
const finalText = toolError ? 'Fixture error reported.' : 'Fixture evidence verified.';
const snapshot = snapshotPersona(JSON.stringify({ schemaVersion: 1, key: 'gpt-fixture', revision: '1', displayName: 'Fixture',
  instructions: 'Read the admitted evidence; report missing information accurately.', skills: [],
  requiredCapabilities: capabilities, optionalCapabilities: [], surfaces: ['task-api'], completionPolicy: completionMode ? 'validated-change' : 'report', effort: 'medium',
  limits: { maxTurns: modelLimit, maxContextBytes: 60000, maxDurationMs: 15000 } }), new Map());
const layers = Object.fromEntries(['tenant', 'principal', 'run', 'surface', 'runtime'].map(key => [key, capabilities]));
const policy = { personaKey: 'gpt-fixture', personaDigest: snapshot.digest, compatibilityClass: 'codex-sdk', harnessRevision: HARNESS_CONTRACT_REVISION,
  canonicalModel: 'gpt-5-codex', allowedEfforts: ['medium'], capabilityLayers: layers,
  limits: { maxTurns: modelLimit, maxContextBytes: 60000, maxDurationMs: 15000 }, deadlineMs: Date.now() + 60000 };
const input = { runId: 'fixture-run', snapshot, policy, source: { kind: 'task-api', taskId: 'tsk_fixture', generation: 1 },
  repository: { provider: 'github', repositoryId: 'fixture/repository', sourceRevision: 'a'.repeat(40) },
  prompt: 'Read fixture evidence and assess the requirement.', maxOutputTokens: 100, maxResponseBytes: 8192, signal: AbortSignal.timeout(30000) };
const before = await readdir(tmpdir());
const requests = [], calls = [], progress = [];
let checks = 0;
const host = {
  async assertCurrent(signal) { signal.throwIfAborted(); if (revoked) throw new Error('fixture revoked'); checks++; },
  ...(completionMode ? { async verifyCompletion() {
    completionChecks++;
    if (completionMode === 'unknown') throw new Error('fixture unknown completion');
    if (completionMode === 'revoked') revoked = true;
    return completionMode === 'repair' && completionChecks === 2;
  } } : {}),
  async progress(event) { progress.push(event); },
  toolBroker: {
    maxCalls: 2, repositoryCapabilities: capabilities,
    definitions: [{ name: 'read_evidence', description: 'Read admitted fixture evidence.', capability: 'repository.read',
      input: z.object({ key: z.string() }), readOnly: true },
      ...capabilities.slice(1).map((capability, index) => ({ name: 'fixture_unused_' + index, description: 'Unused admitted fixture capability.',
        capability, input: z.object({}), readOnly: false }))],
    async execute(name, args, signal) {
      signal.throwIfAborted();
      assert.equal(name, 'read_evidence');
      assert.deepEqual(args, { key: 'story-acceptance' });
      calls.push({ name, args });
      assert.equal(calls.length, 1, 'Host operation replayed');
      return { status: 'confirmed', content: toolText, isError: toolError };
    },
  },
  async model(body) {
    requests.push(body);
    assert.ok(requests.length <= modelLimit, 'Unexpected model replay');
    if (requests.length > 2) {
      assert.ok(JSON.stringify(body.input).includes('host has not verified'));
      assert.ok(JSON.stringify(body.input).includes(toolText), 'Prior tool evidence was lost');
    }
    assert.equal(body.tools.length, 1, 'Native tools leaked into model request');
    assert.equal(body.parallel_tool_calls, false);
    assert.equal(body.tools[0].name, 'mcp__adp');
    assert.equal(body.tools[0].tools[0].name, 'read_evidence');
    const item = requests.length === 1
      ? { id: 'fc_fixture', type: 'function_call', call_id: 'call_fixture', name: 'read_evidence', namespace: 'mcp__adp',
        arguments: JSON.stringify({ key: 'story-acceptance' }), status: 'completed' }
      : { id: 'msg_fixture', type: 'message', role: 'assistant', status: 'completed',
        content: [{ type: 'output_text', text: finalText, annotations: [] }] };
    if (requests.length === 2) {
      assert.equal(calls.length, 1);
      assert.ok(JSON.stringify(body.input).includes(toolText));
    }
    return { operationStatus: 'confirmed', response: { id: `resp_fixture_${requests.length}`, status: 'completed', output: [item],
      usage: { input_tokens: 100, output_tokens: 12 } } };
  },
};
await assert.rejects(runAdmittedSession({ ...input, repository: undefined }, host), /capabilities unavailable/);
await assert.rejects(runAdmittedSession(input, { ...host, toolBroker: undefined }), /capabilities unavailable/);
assert.equal(requests.length, 0);
if (completionMode && completionMode !== 'repair') {
  const error = completionMode === 'revoked' ? /fixture revoked/ : completionMode === 'unknown' ? /fixture unknown completion/ : /completion evidence was not verified/;
  await assert.rejects(runAdmittedSession(input, host), error);
  assert.equal(requests.length, completionMode === 'exhausted' ? 4 : 2);
} else {
  const result = await runAdmittedSession(input, host);
  assert.equal(result.response, finalText);
  assert.equal(requests.length, modelLimit);
  if (completionMode) {
    assert.equal(completionChecks, 2);
    assert.equal(result.usage.input_tokens, 300);
  }
}
assert.equal(calls.length, 1);
assert.ok(checks >= 7);
assert.ok(progress.some(event => event.type === 'tool.started'));
assert.deepEqual((await readdir(tmpdir())).sort(), before.sort(), 'Session storage survived cleanup');
console.log(`Shared tool session: pinned SDK, admitted repository, host MCP execution, exact receipt history, ${toolError ? 'confirmed error' : 'success'} and cleanup passed (fixture inference).`);
