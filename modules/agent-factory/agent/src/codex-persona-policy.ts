/** Fresh model admission for the new shared GitHub Codex adapter only.
 * Existing Claude and native Codex launch paths do not import this module.
 */
import { randomBytes } from 'node:crypto';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { MODEL_POLICY_AUDIENCE, parseVerificationKeys, verifyEnvelope } from './control-envelope';
import { readControlKeyring } from './control-keyring';
import { canonicalJson } from './invocability-probe/canonical-json';
import { gatewaySigningRegion, workerAwsCredentialProvider, workerIdentityHeaders } from './lib/runIdentity';

export class CodexPersonaAdmissionRefused extends Error {
  constructor() { super('Shared Codex persona requires current signed session and model authority'); }
}

export function codexPolicyBody(value: unknown): Buffer {
  return Buffer.from(canonicalJson(value).replace(/[\u007f-\uffff]/g,
    c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`));
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CodexPersonaAdmissionRefused();
  return value as Record<string, unknown>;
}

async function bounded(response: Response, maxBytes?: number): Promise<unknown> {
  if (!response.ok || !response.body) throw new CodexPersonaAdmissionRefused();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (maxBytes !== undefined && (size += value.length) > maxBytes) throw new CodexPersonaAdmissionRefused();
      chunks.push(value);
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } finally { await reader.cancel(); }
}

/** New routes explicitly opt into gateway selection; there is no legacy model.
 * The session endpoint adds a signed capability context unavailable from the
 * legacy model-decision endpoint. Existing runtime postures remain unchanged.
 * Call at every boundary; never reuse a decision after a pause or retry.
 */
export async function admitCodexPersonaModel(persona: string, callerSignal: AbortSignal) {
  const signal = callerSignal;
  try {
    if (process.env.ADP_AGENT_AUTHORITY_ENABLED !== 'true' ||
        !/^agent-codex-(architect|product|pm|operations|aidlc|intent-refinement)$/.test(persona) ||
        persona !== process.env.AGENT_TYPE) throw new CodexPersonaAdmissionRefused();
    const base = process.env.ADP_AGENT_CONTROL_ENDPOINT;
    const runId = process.env.ADP_MESSAGE_ID;
    const tenant = process.env.ADP_TENANT_ID;
    const generation = Number(process.env.ADP_RUN_ATTEMPT);
    if (!base || !/^https:\/\/[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\/[A-Za-z0-9_-]+(?:\/agent)?\/internal\/v1\/agent\/?$/.test(base) ||
        !runId || !tenant || !Number.isSafeInteger(generation) || generation < 1) throw new CodexPersonaAdmissionRefused();
    const nonce = randomBytes(32).toString('hex');
    const url = new URL(base.replace(/\/$/, '') + '/codex-persona-session');
    const body = JSON.stringify({ nonce, model_policy_contract: 1 });
    // The worker proves its protected execution. It never supplies a claimed
    // human, repository, persona or model to the gateway as authorization.
    const operation = async () => {
      const credentials = await workerAwsCredentialProvider();
      const signer = new SignatureV4({ credentials, region: gatewaySigningRegion(url.href),
        service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
      const signed = await signer.sign({ method: 'POST', protocol: url.protocol, hostname: url.hostname,
        path: url.pathname, headers: { host: url.host, 'content-type': 'application/json', ...workerIdentityHeaders() }, body });
      signal.throwIfAborted();
      return bounded(await fetch(url, { method: 'POST', headers: signed.headers, body, redirect: 'error', signal }), 65536);
    };
    // Run cancellation covers credential acquisition and fetch; a late result cannot admit work.
    let abort!: () => void;
    const cancelled = new Promise<never>((_, reject) => {
      abort = () => reject(new CodexPersonaAdmissionRefused());
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
    });
    let document: Record<string, unknown>;
    try { document = object(await Promise.race([operation(), cancelled])); }
    finally { signal.removeEventListener('abort', abort); }
    const result = object(document.result);
    const keys = process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE
      ? readControlKeyring(process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE)
      : parseVerificationKeys(process.env.ADP_CONTROL_ENVELOPE_KEYS);
    const verified = verifyEnvelope(typeof document.assertion === 'string' ? document.assertion : undefined,
      keys, { runId, generation, action: 'model_policy_response', commandId: nonce,
        audience: MODEL_POLICY_AUDIENCE, body: codexPolicyBody(result) });
    if (!verified.ok || verified.envelope.tenantId !== tenant || verified.envelope.principal !== `${runId}#${generation}` ||
        result.invocation_id !== runId || result.tenant_id !== tenant || result.attempt !== generation || result.nonce !== nonce) {
      throw new CodexPersonaAdmissionRefused();
    }
    const policy = object(result.model_policy);
    const decision = object(policy.decision);
    const context = object(object(result.context).codex_persona);
    if (context.version !== 1 || context.persona !== persona ||
        policy.posture_verified !== true || !['enforcing', 'report_only'].includes(String(policy.posture)) || policy.status !== 'proposed' ||
        decision.schema_version !== 1 || decision.runtime_posture !== policy.posture || decision.compatibility_class !== 'codex-sdk' ||
        decision.harness_contract_revision !== '0.155.1' || decision.invocation_id !== runId || decision.tenant_id !== tenant ||
        decision.persona !== persona || typeof decision.resolved_model_id !== 'string' || !decision.resolved_model_id.trim() ||
        typeof decision.snapshot_digest !== 'string' || !/^[a-f0-9]{64}$/.test(decision.snapshot_digest)) {
      throw new CodexPersonaAdmissionRefused();
    }
    signal.throwIfAborted();
    return Object.freeze({ runId, tenant, generation, persona, model: decision.resolved_model_id,
      snapshotDigest: decision.snapshot_digest, evidenceId: nonce, context: structuredClone(context) });
  } catch { throw new CodexPersonaAdmissionRefused(); }
}

/** Protected operation receipt; an ambiguous transport is never retried here. */
export async function codexPersonaOperation(body: { operation_id: string; request_digest: string;
  action: 'claim' | 'settle'; kind: 'model' | 'report' | 'tool' | 'planning'; effect_key?: string; result?: string }, callerSignal: AbortSignal) {
  const signal = callerSignal;
  const base = process.env.ADP_AGENT_CONTROL_ENDPOINT;
  if (!base || !/^https:\/\/[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\/[A-Za-z0-9_-]+(?:\/agent)?\/internal\/v1\/agent\/?$/.test(base)) {
    throw new CodexPersonaAdmissionRefused();
  }
  const url = new URL(base.replace(/\/$/, '') + '/codex-persona-operation');
  const payload = JSON.stringify(body);
  let abort!: () => void;
  const cancelled = new Promise<never>((_, reject) => {
    abort = () => reject(new CodexPersonaAdmissionRefused());
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
  const operation = async () => {
    const credentials = await workerAwsCredentialProvider();
    const signer = new SignatureV4({ credentials, region: gatewaySigningRegion(url.href), service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
    const signed = await signer.sign({ method: 'POST', protocol: url.protocol, hostname: url.hostname, path: url.pathname,
      headers: { host: url.host, 'content-type': 'application/json', ...workerIdentityHeaders() }, body: payload });
    signal.throwIfAborted();
    return object(await bounded(await fetch(url, { method: 'POST', headers: signed.headers, body: payload, redirect: 'error', signal })));
  };
  try { return await Promise.race([operation(), cancelled]); }
  finally { signal.removeEventListener('abort', abort); }
}
