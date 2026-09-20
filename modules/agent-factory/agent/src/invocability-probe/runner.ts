import type { SDKStreamMessage } from '../utils/resilientQuery';
import { resilientQuery } from '../utils/resilientQuery';
import { CLAUDE_ADAPTER_ID, CLAUDE_SDK_VERSION } from '../harnesses/claude-control';
import { startCaptureProxy, type CapturedBedrockRequest, type CaptureProxy } from './capture-proxy';
import { REQUEST_SHAPE_NORMALIZATION } from './canonical-json';
import {
  SigV4ProbeGateway,
  type ProbeClaim,
  type ProbeCompletion,
  type ProbeGateway,
  type ProbeStart,
} from './gateway-client';
import manifest from './request-shape-manifest.json';
import { PROBE_PROMPT, PROBE_PROMPT_SHA256, probeSdkEnvironment, probeSdkOptions } from './request-shape';

const COMPATIBILITY_CLASS = 'claude-agent-sdk';

interface RequestShapeManifest {
  schema_version: number;
  request_shape_normalization: string;
  compatibility_class: string;
  harness_contract_revision: string;
  probe_prompt_sha256: string;
  models: Record<string, string>;
}

export interface HarnessObservation {
  assistantResponses: number;
  resultSubtype: string | null;
  numTurns: number | null;
  totalCostUsd: number | null;
}

export interface ProbeRunResult {
  claimed: boolean;
  slotId?: string;
  outcome?: ProbeCompletion['outcome'];
  reason?: string;
}

export interface ProbeCycleResult {
  completed: number;
  outcomes: Record<ProbeCompletion['outcome'], number>;
  stoppedReason: string;
}

function validateDigest(value: string, field: string): void {
  if (!/^[0-9a-f]{64}$/.test(value)) throw new Error(`${field} is not a lowercase SHA-256 digest`);
}

export function expectedRequestShape(modelId: string): string {
  const checked = manifest as RequestShapeManifest;
  if (checked.schema_version !== 2
      || checked.request_shape_normalization !== REQUEST_SHAPE_NORMALIZATION
      || checked.compatibility_class !== COMPATIBILITY_CLASS
      || checked.harness_contract_revision !== CLAUDE_SDK_VERSION
      || checked.probe_prompt_sha256 !== PROBE_PROMPT_SHA256) {
    throw new Error('checked-in request-shape manifest does not match the Claude probe contract');
  }
  const digest = checked.models[modelId];
  if (!digest) throw new Error(`request-shape manifest has no entry for ${modelId}`);
  validateDigest(digest, 'manifest request shape');
  return digest;
}

function substantiveAssistantResponse(message: SDKStreamMessage): boolean {
  if ((message as { type?: unknown }).type !== 'assistant') return false;
  const content = (message as { message?: { content?: unknown[] } }).message?.content;
  return Array.isArray(content) && content.some((block) => {
    if (!block || typeof block !== 'object') return false;
    const item = block as Record<string, unknown>;
    if (item.type === 'text') return typeof item.text === 'string' && item.text.trim().length > 0;
    return item.type === 'thinking' || item.type === 'tool_use';
  });
}

export async function observeHarness(stream: AsyncIterable<SDKStreamMessage>): Promise<HarnessObservation> {
  let assistantResponses = 0;
  let resultSubtype: string | null = null;
  let numTurns: number | null = null;
  let totalCostUsd: number | null = null;
  for await (const message of stream) {
    if (substantiveAssistantResponse(message)) assistantResponses++;
    if ((message as { type?: unknown }).type === 'result') {
      const result = message as unknown as Record<string, unknown>;
      resultSubtype = typeof result.subtype === 'string' ? result.subtype : null;
      numTurns = typeof result.num_turns === 'number' ? result.num_turns : null;
      totalCostUsd = typeof result.total_cost_usd === 'number' ? result.total_cost_usd : null;
    }
  }
  return { assistantResponses, resultSubtype, numTurns, totalCostUsd };
}

export function classifyObservation(
  harness: HarnessObservation,
  capture: CapturedBedrockRequest,
): ProbeCompletion {
  const digest = capture.requestShapeSha256;
  const maskedSuccess = harness.resultSubtype === 'success'
    && harness.numTurns === 1
    && harness.totalCostUsd === 0
    && harness.assistantResponses === 0;
  if (!capture.forwarded || capture.providerStatus === null || capture.providerStatus >= 400) {
    return {
      outcome: capture.providerStatus !== null && capture.providerStatus >= 400 && capture.providerStatus < 500
        ? 'refused' : 'error',
      request_shape_sha256: digest,
      provider_request_id: capture.providerRequestId,
      error_code: capture.providerErrorCode ?? (capture.forwarded ? 'provider_error' : 'request_shape_mismatch'),
    };
  }
  if (maskedSuccess) {
    return {
      outcome: 'error',
      request_shape_sha256: digest,
      provider_request_id: capture.providerRequestId,
      error_code: 'masked_zero_cost_no_response',
    };
  }
  if (harness.resultSubtype !== 'success' || harness.assistantResponses === 0) {
    return {
      outcome: 'error',
      request_shape_sha256: digest,
      provider_request_id: capture.providerRequestId,
      error_code: 'no_successful_harness_response',
    };
  }
  if (!capture.providerRequestId) {
    return {
      outcome: 'error',
      request_shape_sha256: digest,
      provider_request_id: null,
      error_code: 'missing_provider_request_id',
    };
  }
  return {
    outcome: 'proven',
    request_shape_sha256: digest,
    provider_request_id: capture.providerRequestId,
    error_code: null,
  };
}

function assertClaim(claim: Extract<ProbeClaim, { claimed: true }>): number {
  if (typeof claim.lease_token !== 'string' || claim.lease_token.length < 32 || claim.lease_token.length > 128) {
    throw new Error('Gateway returned an invalid probe lease token');
  }
  if (claim.compatibility_class !== COMPATIBILITY_CLASS) {
    throw new Error(`Gateway selected unsupported compatibility class ${claim.compatibility_class}`);
  }
  if (claim.harness_contract_revision !== CLAUDE_SDK_VERSION) {
    throw new Error(`Gateway selected harness revision ${claim.harness_contract_revision}; runtime is ${CLAUDE_SDK_VERSION}`);
  }
  validateDigest(claim.expected_request_shape_sha256, 'Gateway expected request shape');
  if (claim.expected_request_shape_sha256 !== expectedRequestShape(claim.model_id)) {
    throw new Error('Gateway expected request shape does not match the checked-in manifest');
  }
  const budget = Number(claim.max_budget_usd);
  if (!Number.isFinite(budget) || budget <= 0) throw new Error('Gateway returned a non-positive probe budget');
  if (!Number.isInteger(claim.timeout_seconds) || claim.timeout_seconds <= 0) {
    throw new Error('Gateway returned an invalid probe timeout');
  }
  if (Date.parse(claim.lease_expires_at) <= Date.now()) throw new Error('Gateway probe lease has expired');
  return budget;
}

function assertStarted(
  claim: Extract<ProbeClaim, { claimed: true }>,
  started: ProbeStart,
): void {
  if (started.slot_id !== claim.slot_id || started.model_id !== claim.model_id) {
    throw new Error('Gateway start response does not match the claimed slot');
  }
  if (!started.account_id || !started.region || !started.access_key_id
      || !started.secret_access_key || !started.session_token) {
    throw new Error('Gateway start response omitted destination credentials');
  }
  if (Date.parse(started.credentials_expires_at) <= Date.now()) {
    throw new Error('Gateway returned expired destination credentials');
  }
}

async function invokeHarness(
  claim: Extract<ProbeClaim, { claimed: true }>,
  started: ProbeStart,
  baseUrl: string,
  budget: number,
  controller: AbortController,
): Promise<HarnessObservation> {
  const env = probeSdkEnvironment({
    baseUrl,
    region: started.region,
    accessKeyId: started.access_key_id,
    secretAccessKey: started.secret_access_key,
    sessionToken: started.session_token,
  });
  return observeHarness(resilientQuery({
    queryParams: {
      prompt: PROBE_PROMPT,
      options: probeSdkOptions({
        modelId: claim.model_id,
        maxBudgetUsd: budget,
        abortController: controller,
        env,
      }),
    },
    maxRetries: 0,
    idleTimeoutMs: claim.timeout_seconds * 1000,
    cancellation: {
      signal: controller.signal,
      isCancelled: () => controller.signal.aborted,
      error: () => new Error('Claude invocability probe timed out'),
    },
    log: (line) => console.error(`[invocability-probe] ${line}`),
  }));
}

function boundedErrorCode(error: unknown): string {
  const name = error instanceof Error && error.name ? error.name : 'probe_error';
  return name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 128) || 'probe_error';
}

export async function runOneProbe(gateway: ProbeGateway = new SigV4ProbeGateway()): Promise<ProbeRunResult> {
  const claim = await gateway.claim('scheduled');
  if (!claim.claimed) return { claimed: false, reason: claim.reason };
  const budget = assertClaim(claim);

  // start() is the durable paid-attempt boundary. Credentials do not exist in
  // the worker before this succeeds, so no provider call can precede it.
  const started = await gateway.start(claim.slot_id, claim.lease_token, claim.expected_request_shape_sha256);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), claim.timeout_seconds * 1000);
  let proxy: CaptureProxy | null = null;
  let completion: ProbeCompletion = {
    outcome: 'error',
    request_shape_sha256: claim.expected_request_shape_sha256,
    provider_request_id: null,
    error_code: 'no_request_emitted.proxy_setup_failed',
  };
  try {
    assertStarted(claim, started);
    proxy = await startCaptureProxy({
      modelId: claim.model_id,
      region: started.region,
      credentials: {
        accessKeyId: started.access_key_id,
        secretAccessKey: started.secret_access_key,
        sessionToken: started.session_token,
      },
      expectedRequestShapeSha256: claim.expected_request_shape_sha256,
      signal: controller.signal,
      onRequestRejected: () => controller.abort(),
    });
    const observation = await invokeHarness(claim, started, proxy.baseUrl, budget, controller);
    const captured = await proxy.captured();
    completion = classifyObservation(observation, captured);
  } catch (error) {
    const captured = proxy?.snapshot() ?? null;
    completion = captured
      ? classifyObservation({ assistantResponses: 0, resultSubtype: null, numTurns: null, totalCostUsd: null }, captured)
      : {
        outcome: 'error',
        // The completion contract requires a digest. error_code explicitly
        // tells Gateway this is the expected (not observed) digest; Gateway
        // completes the slot but must not write invocability evidence.
        request_shape_sha256: claim.expected_request_shape_sha256,
        provider_request_id: null,
        error_code: `no_request_emitted.${boundedErrorCode(error)}`.slice(0, 128),
      };
  } finally {
    clearTimeout(timeout);
    controller.abort();
    try { await proxy?.close(); }
    catch (error) { console.error(`[invocability-probe] proxy close failed: ${(error as Error).message}`); }
  }

  await gateway.complete(claim.slot_id, claim.lease_token, completion);
  return { claimed: true, slotId: claim.slot_id, outcome: completion.outcome };
}

/** Drain one Gateway-admitted cycle serially; never runs provider calls in parallel. */
export async function runProbeCycle(
  gateway: ProbeGateway = new SigV4ProbeGateway(),
  maxClaims = 100,
  runOne: (client: ProbeGateway) => Promise<ProbeRunResult> = runOneProbe,
): Promise<ProbeCycleResult> {
  if (!Number.isInteger(maxClaims) || maxClaims <= 0 || maxClaims > 100) {
    throw new Error('probe cycle local claim bound must be between 1 and 100');
  }
  const outcomes: ProbeCycleResult['outcomes'] = { proven: 0, refused: 0, error: 0 };
  for (let completed = 0; completed < maxClaims; completed++) {
    const result = await runOne(gateway);
    if (!result.claimed) return { completed, outcomes, stoppedReason: result.reason ?? 'no_work' };
    if (!result.outcome) throw new Error('claimed probe completed without an outcome');
    outcomes[result.outcome]++;
  }
  // Do not claim slot maxClaims+1 merely to discover whether more work exists:
  // reserving a slot that this process will not execute would strand it.
  throw new Error(`probe cycle reached local hard bound (${maxClaims})`);
}

export const CLAUDE_PROBE_ADAPTER_ID = CLAUDE_ADAPTER_ID;
