/** Fresh gateway authority at the actual Claude SDK launch boundary. */
import { randomBytes } from 'node:crypto';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { query } from '@anthropic-ai/claude-agent-sdk';
import { MODEL_POLICY_AUDIENCE, parseVerificationKeys, verifyEnvelope } from './control-envelope';
import { readControlKeyring } from './control-keyring';
import { canonicalJson } from './invocability-probe/canonical-json';
import { gatewaySigningRegion, workerAwsCredentialProvider, workerIdentityHeaders } from './lib/runIdentity';

type QueryParams = Parameters<typeof query>[0];
type ObjectValue = Record<string, unknown>;

export class ModelPolicyRefused extends Error {
  constructor() { super('Gateway model policy could not authorize this SDK launch'); this.name = 'ModelPolicyRefused'; }
}

function object(value: unknown): ObjectValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ModelPolicyRefused();
  return value as ObjectValue;
}

/** Match the gateway's sorted, ASCII-escaped JSON byte representation. */
export function policyBody(value: unknown): Buffer {
  return Buffer.from(canonicalJson(value).replace(/[\u007f-\uffff]/g,
    c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`));
}

async function readBoundedResponse(response: Response): Promise<unknown> {
  if (!response.ok || !response.body) throw new ModelPolicyRefused();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 65536) throw new ModelPolicyRefused();
      chunks.push(value);
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } finally { await reader.cancel(); }
}

/** Bound credential acquisition as well as transport. A late result never launches an SDK. */
export async function prepareModelQuery(params: QueryParams): Promise<QueryParams> {
  if (process.env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() !== 'true') return params;
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      prepareAuthorizedQuery(params, controller.signal),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new ModelPolicyRefused()); }, 10000);
      }),
    ]);
  } finally { clearTimeout(timer); }
}

/** No cached decision or startup telemetry can grant permission to start. */
async function prepareAuthorizedQuery(params: QueryParams, signal: AbortSignal): Promise<QueryParams> {
  try {
    const base = process.env.ADP_AGENT_CONTROL_ENDPOINT;
    if (!base || !/^https:\/\/[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\/[A-Za-z0-9_-]+\/internal\/v1\/agent\/?$/.test(base)) {
      throw new ModelPolicyRefused();
    }
    const runId = process.env.ADP_MESSAGE_ID;
    const tenant = process.env.ADP_TENANT_ID;
    const generation = Number(process.env.ADP_RUN_ATTEMPT);
    if (!runId || !tenant || !Number.isSafeInteger(generation) || generation < 1) throw new ModelPolicyRefused();
    const url = new URL(base.replace(/\/$/, '') + '/model-decision');
    const nonce = randomBytes(32).toString('hex');
    const body = JSON.stringify({ nonce, model_policy_contract: 1 });
    const started = Date.now();
    const signer = new SignatureV4({ credentials: await workerAwsCredentialProvider(),
      region: gatewaySigningRegion(url.href), service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
    const signed = await signer.sign({ method: 'POST', protocol: url.protocol, hostname: url.hostname,
      path: url.pathname, headers: { host: url.host, 'content-type': 'application/json', ...workerIdentityHeaders() }, body });
    const remaining = 10000 - (Date.now() - started);
    if (remaining <= 0 || signal.aborted) throw new ModelPolicyRefused();
    const response = await fetch(url, { method: 'POST', headers: signed.headers, body,
      redirect: 'error', signal });
    const document = object(await readBoundedResponse(response));
    const result = object(document.result);
    const keys = process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE
      ? readControlKeyring(process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE)
      : parseVerificationKeys(process.env.ADP_CONTROL_ENVELOPE_KEYS);
    const verified = verifyEnvelope(typeof document.assertion === 'string' ? document.assertion : undefined,
      keys, { runId, generation, action: 'model_policy_response', commandId: nonce,
        audience: MODEL_POLICY_AUDIENCE, body: policyBody(result) });
    if (!verified.ok || verified.envelope.tenantId !== tenant ||
        verified.envelope.principal !== `${runId}#${generation}` ||
        result.invocation_id !== runId || result.tenant_id !== tenant || result.attempt !== generation || result.nonce !== nonce ||
        signal.aborted || Date.now() - started >= 10000) throw new ModelPolicyRefused();
    const policy = object(result.model_policy);
    if (policy.posture_verified !== true) throw new ModelPolicyRefused();
    if (policy.posture === 'disabled' || policy.posture === 'report_only') {
      console.info('Model-policy SDK admission', { posture: policy.posture, status: policy.status,
        legacyModel: params.options?.model, enforced: false });
      return params;
    }
    const decision = object(policy.decision);
    if (policy.posture !== 'enforcing' || policy.status !== 'proposed' || decision.schema_version !== 1 ||
        decision.runtime_posture !== 'enforcing' || decision.invocation_id !== runId ||
        decision.tenant_id !== tenant || typeof decision.resolved_model_id !== 'string' || !decision.resolved_model_id ||
        (process.env.AGENT_TYPE && decision.persona !== process.env.AGENT_TYPE)) throw new ModelPolicyRefused();
    const model = decision.resolved_model_id;
    console.info('Model-policy SDK admission', { posture: policy.posture, legacyModel: params.options?.model,
      resolvedModel: model, source: decision.resolution_source, snapshotDigest: decision.snapshot_digest, enforced: true });
    return { ...params, options: { ...params.options, model, fallbackModel: undefined,
      env: { ...(params.options?.env ?? process.env), ANTHROPIC_MODEL: model } } };
  } catch { throw new ModelPolicyRefused(); }
}

/** Used by the two direct SDK consumers that do not need retry orchestration. */
export async function createPolicyQuery(params: QueryParams): Promise<ReturnType<typeof query>> {
  return query(await prepareModelQuery(params));
}
