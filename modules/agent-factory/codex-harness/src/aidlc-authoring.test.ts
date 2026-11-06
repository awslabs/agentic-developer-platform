import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { z } from 'zod';
import { COMMON_RULES, phaseRules, projectRules } from './projection.js';
import { snapshotPersona } from './persona.js';
import { runAdmittedSession } from './session.js';
import { HARNESS_CONTRACT_REVISION } from './admission.js';

test('AI-DLC SDK receives authoring rules and gate restrictions within the Task request budget', async () => {
  const rules = fileURLToPath(new URL('../../rules/', import.meta.url));
  const raw = JSON.parse(readFileSync(new URL('../personas/aidlc.json', import.meta.url), 'utf8'));
  const projection = projectRules(rules, 'aidlc', [...COMMON_RULES, 'personas/aidlc.md', ...phaseRules(rules, 'aidlc')]);
  const snapshot = snapshotPersona(JSON.stringify({ ...raw, sharedRules: {
    ...projection, sources: projection.sources.map(({ path, sha256 }) => ({ path, sha256 })),
  } }), new Map());
  const caps = ['agents.delegate', 'artifacts.publish'] as const;
  let captured: string | undefined;
  try {
    await runAdmittedSession({
      runId: 'aidlc-authoring-fixture', snapshot,
      policy: { personaKey: raw.key, personaDigest: snapshot.digest, compatibilityClass: 'codex-sdk',
        harnessRevision: HARNESS_CONTRACT_REVISION, canonicalModel: 'fixture', allowedEfforts: [raw.effort],
        capabilityLayers: { tenant: caps, principal: caps, run: caps, surface: caps, runtime: caps },
        limits: raw.limits, deadlineMs: Date.now() + 25000 },
      source: { kind: 'task-api', taskId: 'task-fixture', generation: 1 },
      prompt: 'Draft a bounded invitation story after the authorized planning gate.',
      maxOutputTokens: 512, maxResponseBytes: 48000, signal: AbortSignal.timeout(25000),
    }, {
      async assertCurrent() {}, async progress() {},
      async verifyCompletion() { throw new Error('Fixture cannot accept work'); },
      toolBroker: { maxCalls: 1, repositoryCapabilities: ['agents.delegate'],
        definitions: [{ name: 'delegate', capability: 'agents.delegate', input: z.object({}),
          readOnly: false, description: 'Fixture; never executed' }],
        async execute() { throw new Error('Fixture cannot dispatch'); } },
      async model(request) { captured = JSON.stringify(request); throw new Error('Capture only'); },
    });
  } catch (error) { if (!captured) throw error; }
  assert.ok(captured);
  assert.ok(Buffer.byteLength(captured) <= 63 * 1024);
  for (const required of ['Epic/story authoring contract', 'EC-01', 'AC-01', 'Given/When/Then/Controls',
    'Module IDs', 'Feature IDs', 'Amendment mode produces an inert draft only']) {
    assert.ok(captured.includes(required), `Missing ${required} in actual SDK request`);
  }
});
