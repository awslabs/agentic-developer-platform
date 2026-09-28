/** Fresh gateway authority at the actual Claude SDK launch boundary. */
import { CLAUDE_SDK_VERSION } from './harnesses/claude-control';
import { arcIdentity } from './arc-model-identity';
import { randomBytes } from 'node:crypto';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { query } from '@anthropic-ai/claude-agent-sdk';
import { MODEL_POLICY_AUDIENCE, parseVerificationKeys, verifyEnvelope } from './control-envelope';
import { readControlKeyring } from './control-keyring';
import { canonicalJson } from './invocability-probe/canonical-json';
import { gatewaySigningRegion, readIdentityToken, workerAwsCredentialProvider, workerIdentityHeaders } from './lib/runIdentity';

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
  if (process.env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() !== 'true' &&
      process.env.ADP_CHAT_MODEL_POLICY_ENABLED?.toLowerCase() !== 'true' &&
      process.env.ADP_ARC_MODEL_POLICY_ENABLED?.toLowerCase() !== 'true') return params;
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

/** Preserve every model option while binding gateway accounting to this launch. */
function withUsageEvidence(params: QueryParams, nonce: string): QueryParams {
  const env = params.options?.env ?? process.env;
  const existing = (env.ANTHROPIC_CUSTOM_HEADERS ?? '').split('\n')
    .filter(line => !/^x-adp-model-evidence\s*:/i.test(line) && line.trim());
  return { ...params, options: { ...params.options, env: { ...env,
    ANTHROPIC_CUSTOM_HEADERS: [...existing, `X-Adp-Model-Evidence: ${nonce}`].join('\n'),
  } } };
}

/** No cached decision or startup telemetry can grant permission to start. */
async function prepareAuthorizedQuery(params: QueryParams, signal: AbortSignal): Promise<QueryParams> {
  try {
    const base = process.env.ADP_AGENT_CONTROL_ENDPOINT;
    const arc = process.env.ADP_ARC_MODEL_POLICY_ENABLED?.toLowerCase() === 'true';
    const chat = process.env.ADP_MODEL_POLICY_SOURCE === 'chat';
    if (!base || !/^https:\/\/[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\/[A-Za-z0-9_-]+(?:\/agent)?\/internal\/v1\/agent(?:\/chat|\/arc)?\/?$/.test(base) ||
        base.replace(/\/$/, '').endsWith('/chat') !== chat || (arc && !base.replace(/\/$/, '').endsWith('/arc'))) {
      throw new ModelPolicyRefused();
    }
    let runId = process.env.ADP_MESSAGE_ID;
    let tenant = process.env.ADP_TENANT_ID;
    let generation = Number(process.env.ADP_RUN_ATTEMPT);
    if (!arc && (!runId || !tenant || !Number.isSafeInteger(generation) || generation < 1)) throw new ModelPolicyRefused();
    const url = new URL(base.replace(/\/$/, '') + '/model-decision');
    const nonce = randomBytes(32).toString('hex');
    const digest = process.env.ADP_MODEL_ROOT_ENVELOPE_DIGEST;
    if (chat && (!digest || !/^[0-9a-f]{64}$/.test(digest))) throw new ModelPolicyRefused();
    let body = JSON.stringify({ nonce, model_policy_contract: 1,
      ...(chat ? { invocation_id: runId, envelope_digest: digest } : {}) });
    const started = Date.now();
    const credentials = await workerAwsCredentialProvider();
    const arcRequest = arc ? await arcIdentity(nonce, gatewaySigningRegion(url.href), credentials, signal) : undefined;
    if (arcRequest) body = arcRequest.body;
    const signer = new SignatureV4({ credentials,
      region: gatewaySigningRegion(url.href), service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
    const signed = await signer.sign({ method: 'POST', protocol: url.protocol, hostname: url.hostname,
      path: url.pathname, headers: { host: url.host, 'content-type': 'application/json',
        ...(arcRequest ? arcRequest.headers : chat ? { 'X-Adp-Workload-Token': readIdentityToken(process.env.ADP_WORKLOAD_TOKEN_FILE) } : workerIdentityHeaders()) }, body });
    const remaining = 10000 - (Date.now() - started);
    if (remaining <= 0 || signal.aborted) throw new ModelPolicyRefused();
    const response = await fetch(url, { method: 'POST', headers: signed.headers, body,
      redirect: 'error', signal });
    const document = object(await readBoundedResponse(response));
    const result = object(document.result);
    if (arc) {
      // Only the signed response to this fresh nonce may supply ARC ownership.
      runId = typeof result.invocation_id === 'string' ? result.invocation_id : undefined;
      tenant = typeof result.tenant_id === 'string' ? result.tenant_id : undefined;
      generation = Number(result.attempt);
    }
    if (!runId || !tenant || !Number.isSafeInteger(generation) || generation < 1) throw new ModelPolicyRefused();
    let discovered: string | undefined;
    if ((arc || chat) && !process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE && !process.env.ADP_CONTROL_ENVELOPE_KEYS) {
      const keyUrl = new URL(url.href.replace(/\/(?:chat|arc)\/model-decision$/, '/model-policy-keys'));
      const keyRequest = await signer.sign({ method: 'GET', protocol: keyUrl.protocol, hostname: keyUrl.hostname,
        path: keyUrl.pathname, headers: { host: keyUrl.host } });
      const keyResponse = await fetch(keyUrl, { headers: keyRequest.headers, redirect: 'error', signal });
      discovered = JSON.stringify(object(await readBoundedResponse(keyResponse)).keys);
    }
    const keys = process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE
      ? readControlKeyring(process.env.ADP_CONTROL_ENVELOPE_KEYS_FILE)
      : parseVerificationKeys(discovered || process.env.ADP_CONTROL_ENVELOPE_KEYS);
    const verified = verifyEnvelope(typeof document.assertion === 'string' ? document.assertion : undefined,
      keys, { runId, generation, action: 'model_policy_response', commandId: nonce,
        audience: MODEL_POLICY_AUDIENCE, body: policyBody(result) });
    if (!verified.ok || verified.envelope.tenantId !== tenant ||
        verified.envelope.principal !== `${runId}#${generation}` ||
        result.invocation_id !== runId || result.tenant_id !== tenant || result.attempt !== generation || result.nonce !== nonce ||
        signal.aborted || Date.now() - started >= 10000) throw new ModelPolicyRefused();
    if (arc) {
      const context = object(result.context);
      const github = object(context.github_actions);
      if (context.persona !== process.env.AGENT_TYPE || github.repository_id !== process.env.GITHUB_REPOSITORY_ID ||
          github.run_id !== process.env.GITHUB_RUN_ID || github.run_attempt !== process.env.GITHUB_RUN_ATTEMPT ||
          github.workflow_ref !== process.env.GITHUB_WORKFLOW_REF) throw new ModelPolicyRefused();
    }
    const policy = object(result.model_policy);
    if (policy.posture_verified !== true) throw new ModelPolicyRefused();
    if (policy.posture === 'disabled' || policy.posture === 'report_only') {
      console.info('Model-policy SDK admission', { posture: policy.posture, status: policy.status,
        legacyModel: params.options?.model, enforced: false });
      if (policy.posture === 'report_only') {
        const decision = policy.decision && typeof policy.decision === 'object' && !Array.isArray(policy.decision)
          ? policy.decision as ObjectValue : {};
        // Selection evidence at this fresh SDK boundary, not a provider receipt.
        // The nonce joins PMM-08 usage when that launch reaches the gateway ledger.
        console.info('PMM09_MODEL_SHADOW ' + JSON.stringify({
          event: 'persona_model_shadow_comparison', schema_version: 1, phase: 'sdk_admission',
          invocation_id: runId, tenant_id: tenant, attempt: generation, model_decision_id: nonce,
          timestamp_utc: new Date().toISOString(),
          channel: arc ? 'github' : chat ? 'chat' : process.env.ADP_DISPATCH_CHANNEL,
          trigger: arc ? 'arc_workflow' : chat ? 'chat' : process.env.ADP_DISPATCH_TRIGGER,
          persona: decision.persona, principal_kind: decision.principal_kind, principal_id: decision.principal_id,
          legacy_model: params.options?.model, actual_model: params.options?.model, proposed_model: decision.resolved_model_id,
          mapping_exists: decision.resolution_source === 'principal-mapping', resolution_source: decision.resolution_source,
          policy_revision: decision.policy_revision, snapshot_digest: decision.snapshot_digest,
          posture_revision: decision.posture_revision, runtime_posture: policy.posture,
          posture_verified: policy.posture_verified, policy_status: policy.status, admission_refusal: false,
        }));
      }
      return withUsageEvidence(params, nonce);
    }
    const decision = object(policy.decision);
    if (policy.posture !== 'enforcing' || policy.status !== 'proposed' || decision.schema_version !== 1 ||
        decision.runtime_posture !== 'enforcing' || decision.compatibility_class !== 'claude-agent-sdk' ||
        decision.harness_contract_revision !== CLAUDE_SDK_VERSION || decision.invocation_id !== runId ||
        decision.tenant_id !== tenant || typeof decision.resolved_model_id !== 'string' || !decision.resolved_model_id ||
        (process.env.AGENT_TYPE && decision.persona !== process.env.AGENT_TYPE)) throw new ModelPolicyRefused();
    const model = decision.resolved_model_id;
    console.info('Model-policy SDK admission', { posture: policy.posture, legacyModel: params.options?.model,
      resolvedModel: model, source: decision.resolution_source, snapshotDigest: decision.snapshot_digest, enforced: true });
    return withUsageEvidence({ ...params, options: { ...params.options, model, fallbackModel: undefined,
      env: { ...(params.options?.env ?? process.env), ANTHROPIC_MODEL: model } } }, nonce);
  } catch { throw new ModelPolicyRefused(); }
}

/** Used by the two direct SDK consumers that do not need retry orchestration. */
export async function createPolicyQuery(params: QueryParams): Promise<ReturnType<typeof query>> {
  return query(await prepareModelQuery(params));
}
