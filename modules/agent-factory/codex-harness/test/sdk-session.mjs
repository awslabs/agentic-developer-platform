/** Real shared session + pinned SDK + bridge; host inference is a local fixture. */
import assert from 'node:assert/strict';
import { readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { snapshotPersona } from '../dist/persona.js';
import { HARNESS_CONTRACT_REVISION } from '../dist/admission.js';
import { runAdmittedSession } from '../dist/session.js';

const snapshot = snapshotPersona(JSON.stringify({ schemaVersion: 1, key: 'gpt-fixture', revision: '1', displayName: 'Fixture',
  instructions: 'Use fixture evidence and report uncertainties. Do not use tools.', skills: [],
  requiredCapabilities: ['artifacts.publish'], optionalCapabilities: [], surfaces: ['task-api'], completionPolicy: 'report', effort: 'medium',
  limits: { maxTurns: 2, maxContextBytes: 60000, maxDurationMs: 15000 } }), new Map());
const layers = Object.fromEntries(['tenant', 'principal', 'run', 'surface', 'runtime'].map(key => [key, ['artifacts.publish']]));
const policy = { personaKey: 'gpt-fixture', personaDigest: snapshot.digest, compatibilityClass: 'codex-sdk', harnessRevision: HARNESS_CONTRACT_REVISION,
  canonicalModel: 'gpt-5-codex', allowedEfforts: ['medium'], capabilityLayers: layers,
  limits: { maxTurns: 2, maxContextBytes: 60000, maxDurationMs: 15000 }, deadlineMs: Date.now() + 60000 };
const input = { runId: 'fixture-run', snapshot, policy, source: { kind: 'task-api', taskId: 'tsk_fixture', generation: 1 },
  prompt: 'Reply with the fixture conclusion.', maxOutputTokens: 100, maxResponseBytes: 8192, signal: AbortSignal.timeout(30000) };
const before = await readdir(tmpdir());
let checks = 0;
let calls = 0;
const progress = [];
const host = {
  marker: true,
  async assertCurrent(signal) { signal.throwIfAborted(); checks++; },
  async model(request) {
    calls++;
    assert.ok(JSON.stringify(request).includes(snapshot.instructions), 'Pinned persona instructions missing from actual SDK request');
    assert.equal('model' in request, false);
    assert.equal('tools' in request, false);
    return { operationStatus: 'confirmed', response: { id: 'resp_fixture', status: 'completed', output: [
      { id: 'rs_fixture', type: 'reasoning', encrypted_content: 'fixture-ciphertext', summary: [] },
      { id: 'msg_fixture', type: 'message', role: 'assistant', status: 'completed', phase: 'final_answer', content: [{ type: 'output_text', text: 'Fixture conclusion.', annotations: [] }] },
    ], usage: { input_tokens: 100, output_tokens: 10 } } };
  },
  async progress(event) { assert.equal(this.marker, true); progress.push(event); },
};
const evidence = await runAdmittedSession(input, host);
assert.equal(evidence.response, 'Fixture conclusion.');
assert.equal(calls, 1);
assert.equal(checks, 4);
assert.deepEqual(progress, [{ type: 'turn.started' }]);
assert.deepEqual((await readdir(tmpdir())).sort(), before.sort(), 'Session files survived cleanup');
await assert.rejects(runAdmittedSession({ ...input, snapshot: { ...snapshot, instructions: 'tampered' } }, host), /instruction binding/);
assert.equal(calls, 1);
await assert.rejects(runAdmittedSession(input, { ...host, async assertCurrent() { throw new Error('fixture revoked'); } }), /fixture revoked/);
assert.equal(calls, 1);
console.log('Shared runtime: real SDK provisioning, persona instructions, live-grant callbacks, reasoning response, progress and cleanup passed. Fixture inference only.');
