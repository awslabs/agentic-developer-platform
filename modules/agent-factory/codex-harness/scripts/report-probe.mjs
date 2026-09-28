/** Capture the actual GitHub report adapter request without provider access. */
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { runAdmittedSession } from '../dist/session.js';
import { githubTools } from '../dist/github-tools.js';
import { verifySnapshot } from '../dist/persona.js';
import { HARNESS_CONTRACT_REVISION } from '../dist/admission.js';

export async function captureReportProbe(persona, model) {
  const catalogue = JSON.parse(await readFile(new URL('./report-probe-catalogue.json', import.meta.url)));
  const snapshot = catalogue.snapshots.find(s => JSON.parse(s.definition).key === persona.replace('agent-codex-', 'gpt-'));
  if (!snapshot) throw new Error('Unknown report probe persona');
  verifySnapshot(snapshot);
  const definition = JSON.parse(snapshot.definition);
  const capabilities = ['artifacts.publish'];
  if ([...definition.requiredCapabilities, ...definition.optionalCapabilities].includes('repository.read')) capabilities.push('repository.read');
  const layers = Object.fromEntries(['tenant', 'principal', 'run', 'surface', 'runtime'].map(k => [k, capabilities]));
  const tools = githubTools({ persona, repository: 'probe/repository', revision: '0'.repeat(40), capabilities }, async () => { throw new Error('Probe tools cannot execute'); });
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
      ...(tools.definitions.length ? { toolBroker: { definitions: tools.definitions, repositoryCapabilities: ['repository.read'], maxCalls: 1,
        async execute() { throw new Error('Probe tools cannot execute'); } } } : {}),
      async model(request) { calls++; body = { ...request, model, stream: false, store: false, include: ['reasoning.encrypted_content'] }; throw new Error('Capture only'); },
    });
  } catch (error) { if (!body) throw error; }
  if (calls !== 1 || !body) throw new Error('Expected one SDK request');
  // The shared bridge already removes SDK identity metadata. Normalize only
  // its synthetic temporary environment, clock and equivalent UTC spellings.
  for (const message of body.input) for (const part of message.content ?? []) {
    if (part.type !== 'input_text' || typeof part.text !== 'string') continue;
    if (part.text.startsWith('<environment_context>') || part.text.startsWith('<skills_instructions>'))
      part.text = part.text.replace(/\/[^<>\s]*adp-codex-session-[^/<>\s]+/g, '/adp-report-probe');
    if (part.text.startsWith('<environment_context>')) part.text = part.text
      .replace(/<current_date>\d{4}-\d{2}-\d{2}<\/current_date>/, '<current_date>2026-01-01</current_date>')
      .replace(/<timezone>(?:Etc\/UTC|\/UTC|UTC)<\/timezone>/, '<timezone>UTC</timezone>');
  }
  function canonical(v) { return Array.isArray(v) ? '[' + v.map(canonical).join(',') + ']' : v && typeof v === 'object'
    ? '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}' : JSON.stringify(v); }
  const serialized = canonical(body);
  if (Buffer.byteLength(serialized) > 65536) throw new Error('Report probe exceeds bound');
  return { body: serialized, digest: createHash('sha256').update(serialized).digest('hex') };
}
