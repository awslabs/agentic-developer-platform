/** Capture the actual GitHub report adapter request without provider access. */
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { runAdmittedSession } from '../dist/session.js';
import { githubTools } from '../dist/github-tools.js';
import { verifySnapshot } from '../dist/persona.js';
import { HARNESS_CONTRACT_REVISION } from '../dist/admission.js';

/** Registered GitHub report personas and the catalogue definition each one runs. */
export const REPORT_PROBE_PERSONAS = Object.freeze({
  'agent-codex-architect': 'gpt-architect',
  'agent-codex-product': 'gpt-product',
  'agent-codex-pm': 'gpt-pm',
  'agent-codex-intent-refinement': 'gpt-intent-refinement',
  // The engine-dispatched operations story worker runs the operations-story
  // definition, not a `gpt-operations` catalogue entry (#7574).
  'agent-codex-operations': 'gpt-operations-story',
});
export function reportProbeKey(persona) {
  const key = REPORT_PROBE_PERSONAS[persona];
  if (!key) throw new Error('Unknown report probe persona');
  return key;
}
export async function captureReportProbe(persona, model) {
  const catalogue = JSON.parse(await readFile(new URL('./report-probe-catalogue.json', import.meta.url)));
  const snapshot = catalogue.snapshots.find(s => JSON.parse(s.definition).key === reportProbeKey(persona));
  if (!snapshot) throw new Error('Unknown report probe persona');
  verifySnapshot(snapshot);
  const definition = JSON.parse(snapshot.definition);
  // The probe must be admitted under the capabilities the gateway grants the
  // real run: every capability the definition requires, plus repository.read
  // when the persona may read the repository. Report personas require only
  // artifacts.publish, so their captured shape is unchanged.
  const capabilities = [...new Set(['artifacts.publish', ...definition.requiredCapabilities,
    ...([...definition.requiredCapabilities, ...definition.optionalCapabilities].includes('repository.read') ? ['repository.read'] : [])])];
  const layers = Object.fromEntries(['tenant', 'principal', 'run', 'surface', 'runtime'].map(k => [k, capabilities]));
  // The operations tool surface is gated on the executor flag the worker pod
  // carries; mirror it for the capture so the probe sees the run's tool set.
  const executorFlag = process.env.ADP_ENGINE_OPERATIONS_EXECUTOR;
  if (persona === 'agent-codex-operations') process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = 'true';
  let tools;
  try {
    tools = githubTools({ persona, repository: 'probe/repository', revision: '0'.repeat(40), capabilities }, async () => { throw new Error('Probe tools cannot execute'); });
  } finally {
    if (executorFlag === undefined) delete process.env.ADP_ENGINE_OPERATIONS_EXECUTOR; else process.env.ADP_ENGINE_OPERATIONS_EXECUTOR = executorFlag;
  }
  let body; let calls = 0;
  try {
    await runAdmittedSession({ runId: 'report-probe', snapshot,
      policy: { personaKey: definition.key, personaDigest: snapshot.digest, compatibilityClass: 'codex-sdk',
        harnessRevision: HARNESS_CONTRACT_REVISION, canonicalModel: model, allowedEfforts: [definition.effort],
        capabilityLayers: layers, limits: definition.limits, deadlineMs: Date.now() + 25000 },
      repository: { provider: 'github', repositoryId: '1', sourceRevision: '0'.repeat(40) },
      source: { kind: 'github', eventId: 'report-probe' },
      prompt: 'This is a bounded model invocability check. Reply with exactly OK. Do not call any tools.',
      maxOutputTokens: 512, maxResponseBytes: 48000, signal: AbortSignal.timeout(25000),
    }, { async assertCurrent() {}, async progress() {},
      ...(tools.definitions.length ? { toolBroker: { definitions: tools.definitions, repositoryCapabilities: capabilities.filter(c => c !== 'artifacts.publish'), maxCalls: 1,
        async execute() { throw new Error('Probe tools cannot execute'); } } } : {}),
      // The small availability probe retains its bounded JSON response contract.
      // Production report execution consumes provider SSE through github-entry.
      async model(request) { calls++; body = { ...request, model, stream: false, store: false, include: ['reasoning.encrypted_content'] }; throw new Error('Capture only'); },
    });
  } catch (error) { if (!body) throw error; }
  if (calls !== 1 || !body) throw new Error('Expected one SDK request');
  normalizeReportProbeEnvironment(body);
  function canonical(v) { return Array.isArray(v) ? '[' + v.map(canonical).join(',') + ']' : v && typeof v === 'object'
    ? '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}' : JSON.stringify(v); }
  const serialized = canonical(body);
  // Keep the inline PM contract within the existing request budget and the
  // native runner's 128 KiB stdout buffer after JSON escaping.
  if (Buffer.byteLength(serialized) > 65536) throw new Error('Report probe exceeds bound');
  const captured = { body: serialized, digest: createHash('sha256').update(serialized).digest('hex') };
  if (Buffer.byteLength(JSON.stringify(captured)) > 128 * 1024) throw new Error('Report probe capture exceeds transport bound');
  return captured;
}

/** Ignore only host-dependent fields in this synthetic invocability probe. */
export function normalizeReportProbeEnvironment(body) {
  // The shared bridge removes SDK identity metadata. Normalize only the
  // synthetic host environment; retain instructions, permissions and tools.
  for (const message of body.input) for (const part of message.content ?? []) {
    if (part.type !== 'input_text' || typeof part.text !== 'string') continue;
    // Writable tool surfaces add a <permissions instructions> message naming the
    // synthetic session directory; normalize it like the other environment parts.
    if (part.text.startsWith('<environment_context>') || part.text.startsWith('<skills_instructions>') || part.text.startsWith('<permissions instructions>'))
      part.text = part.text.replace(/\/[^<>\s`]*adp-codex-session-[^/<>\s`]+/g, '/adp-report-probe');
    // macOS resolves the SDK's standard /tmp writable root through /private.
    if (part.text.startsWith('<permissions instructions>'))
      part.text = part.text.replace(/`\/private\/tmp`/g, '`/tmp`');
    if (part.text.startsWith('<environment_context>')) part.text = part.text
      .replace(/<current_date>\d{4}-\d{2}-\d{2}<\/current_date>/, '<current_date>2026-01-01</current_date>')
      .replace(/<timezone>[^<>]*<\/timezone>/, '<timezone>UTC</timezone>')
      .replace(/<shell>[^<>]*<\/shell>/, '<shell>bash</shell>');
  }
}
