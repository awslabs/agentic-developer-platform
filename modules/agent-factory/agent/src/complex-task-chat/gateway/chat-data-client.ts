import { createHash, randomBytes } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { z } from 'zod';
import { MODEL_POLICY_AUDIENCE, parseVerificationKeys, verifyEnvelope } from '../../control-envelope';
import { policyBody } from '../../model-policy-body';
import { canonicalJson } from '../../invocability-probe/canonical-json';
import { qualificationClaim, qualificationFixtures, qualificationPlan, qualificationResult, QualificationFixtures } from '../qualification-contract';
import { validateBaseUrl } from '../../lib/url-guard';
import { AnswerEvidence, answerEvidenceSchema } from './answer-evidence';
import { ChatModelRequest, modelOutputBlock, modelRequestSchema, type ModelStreamEvent } from './chat-model-contract';
import { MODEL_STREAM_TYPE, ModelStreamBinding, ModelStreamError, ModelTextListener, readModelStream } from './chat-model-stream';

const identifier = z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/);
const bootstrapSchema = z.object({
  capability: z.string().min(1).max(4096).regex(/^[A-Za-z0-9_.-]+$/),
  run_id: identifier,
  session_id: identifier,
  attempt: z.number().int().positive().optional(),
  lease_generation: z.number().int().positive().optional(),
  expires_at: z.number().int().positive(),
  session_mode: z.enum(['ephemeral', 'persistent']).default('ephemeral'),
  qualification_required: z.literal(true).optional(),
}).strict();
const sessionStateSchema = z.object({
  mode: z.enum(['ephemeral', 'persistent']),
  sequence: z.number().int().nonnegative(),
  health: z.enum(['idle', 'active', 'ending', 'ended', 'recovering', 'cleanup_delayed']),
  pending_mode: z.enum(['ephemeral', 'persistent']).optional(),
  cleanup_elapsed_seconds: z.number().int().nonnegative().optional(),
}).strict();
const sessionNextSchema = z.object({
  run_id: identifier,
  session_id: identifier,
  lease_generation: z.number().int().positive(),
  turn: z.object({
    sequence: z.number().int().positive(),
    turn_id: identifier,
    message: z.string().min(1).max(65_536),
  }).strict().nullable(),
}).strict();
const acceptedTurnSchema = z.object({
  run_id: identifier,
  session_id: identifier,
  lease_generation: z.number().int().positive(),
  session_sequence: z.number().int().positive().optional(),
  turn: z.object({
    ref: z.string().regex(/^user_[a-f0-9]{64}$/),
    message: z.object({
      role: z.literal('user'),
      content: z.string().max(131_072),
      ts: z.string().min(1).max(128),
      tokens: z.number().int().nonnegative(),
      parts: z.array(z.object({
        type: z.literal('file'),
        artifactId: z.string().regex(/^art_[A-Za-z0-9_.:-]{1,128}$/),
      }).strict()).max(32),
    }).strict(),
  }).strict(),
  // Gateway-rendered system prompt and its content-addressed revision (#8113); both or neither.
  system_prompt: z.string().min(1).max(16_000).optional(),
  prompt_revision: z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/).optional(),
}).strict().refine(turn => (turn.system_prompt === undefined) === (turn.prompt_revision === undefined));

const missingArtifactErrorSchema = z.object({
  detail: z.object({ error: z.literal('chat_artifact_missing') }).strict(),
}).strict();
/**
 * The signed decision body, per chat model-policy posture (#7997). `enforcing` is the
 * fail-closed path and must be verified. `advisory` and `off` are the gateway's relaxed
 * postures: the decision carries the matching report_only / disabled runtime posture, may be
 * unverified, and names the rejected check (`rejection_reason`) when evidence was refused.
 * Anything else (a bare `report_only`, an unverified `enforcing`, a mismatched runtime
 * posture) is not a decision this sandbox may act on.
 */
const policyReason = z.string().regex(/^[a-z0-9_]{1,64}$/);
const modelDecisionSchema = (runtimePosture: 'enforcing' | 'report_only' | 'disabled') => z.object({
  runtime_posture: z.literal(runtimePosture),
  principal_kind: z.literal('human'), principal_id: identifier,
  invocation_id: identifier,
  tenant_id: identifier,
  resolved_model_id: z.string().min(1),
}).passthrough();
const modelPolicySchema = z.discriminatedUnion('posture', [
  z.object({
    posture: z.literal('enforcing'), posture_verified: z.literal(true), status: z.literal('proposed'),
    decision: modelDecisionSchema('enforcing'),
  }).passthrough(),
  z.object({
    posture: z.literal('advisory'), posture_verified: z.boolean(), status: z.literal('proposed'),
    rejection_reason: policyReason.optional(), decision: modelDecisionSchema('report_only'),
  }).passthrough(),
  z.object({
    posture: z.literal('off'), posture_verified: z.boolean(), status: z.literal('proposed'),
    rejection_reason: policyReason.optional(), decision: modelDecisionSchema('disabled'),
  }).passthrough(),
]);
export type ModelPolicyPosture = z.infer<typeof modelPolicySchema>['posture'];
const modelReplySchema = z.object({
  result: z.object({
    nonce: z.string().regex(/^[0-9a-f]{64}$/),
    invocation_id: identifier,
    tenant_id: identifier,
    attempt: z.number().int().positive(),
    context: z.object({ lease_generation: z.number().int().positive() }).passthrough(),
    model_policy: modelPolicySchema,
  }).passthrough(),
  assertion: z.string().min(1).max(8192),
}).strict();
const modelKeysSchema = z.object({ keys: z.record(z.string(), z.string().min(1).max(4096)) }).strict();
const textModelRequestSchema = z.object({
  messages: z.array(z.object({
    role: z.enum(['user', 'assistant']), content: z.string().min(1).max(32_000),
  }).strict()).min(1).max(32),
  system: z.string().max(16_000).optional(),
  max_tokens: z.number().int().min(1).max(10_000),
}).strict();
const modelReceiptSchema = z.object({
  run_id: identifier, session_id: identifier, operation_id: identifier,
  request_digest: z.string().regex(/^[a-f0-9]{64}$/), model_id: z.string().min(1).max(256),
  lease_generation: z.number().int().positive(),
  status: z.enum(['pending', 'running', 'confirmed', 'unknown', 'rejected']),
  handoff: z.enum(['not_started', 'prepared', 'confirmed', 'unknown']),
  reservation_status: z.enum(['unknown', 'pending', 'reserved', 'settled', 'not_reserved', 'released']),
  usage: z.object({
    input_tokens: z.number().int().nonnegative(), output_tokens: z.number().int().nonnegative(),
    estimated_usd: z.string().regex(/^\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?$/),
  }).strict().nullable(),
  automatic_replay_permitted: z.literal(false),
  content: z.array(modelOutputBlock).min(1).max(64).optional(),
  stop_reason: z.enum(['end_turn', 'max_tokens', 'stop_sequence', 'tool_use']).optional(),
  error_code: z.string().max(128).optional(),
  // Class and bounded operator-facing message of the exception that ended a rejected or
  // unknown operation (#8048). Gateways before that change omit both; the bound is wider
  // than the gateway's 300 code points so an astral character never fails the receipt.
  error_class: z.string().max(128).optional(),
  error_detail: z.string().max(1024).optional(),
}).strict();

export type TextModelRequest = z.infer<typeof textModelRequestSchema>;

/**
 * Why a model invocation ended without a confirmed receipt: the gateway refused it before
 * the provider call (`rejected`) or lost track of it after (`unknown`). Carried on the
 * `denied` / `incomplete` ChatDataError so the turn can name the reason to the owner (#8048).
 */
export interface ModelInvokeRefusal {
  status: 'rejected' | 'unknown';
  error_code?: string;
  error_class?: string;
  error_detail?: string;
}

/** Owner-visible text for a failed turn (same trust level as a recorded reply); bounded like the gateway's `TurnResult.error_message`. */
export const TURN_ERROR_MESSAGE_MAX_LENGTH = 1024;
const turnErrorMessage = z.string().min(1).max(TURN_ERROR_MESSAGE_MAX_LENGTH).regex(/^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$/);
const turnResultSchema = z.discriminatedUnion('outcome', [
  z.object({ outcome: z.literal('completed'), message_id: identifier,
    reply_granularity: z.literal('row').optional(), evidence: answerEvidenceSchema.optional() }).strict(),
  z.object({ outcome: z.literal('failed'), error_class: z.enum(['executor_not_wired', 'executor_down']).optional(),
    error_message: turnErrorMessage.optional() }).strict(),
]);

type Binding = z.infer<typeof bootstrapSchema>;
const sessionOperations = {
  'history/read': ['limit', 'cursor'],
  'history/messages': ['ids'],
  'history/summary': ['summary_id'],
  'history/turn': [],
  'history/append': ['idempotency_key', 'expected_version', 'content', 'tokens', 'user_turn_id'],
  'history/summary/append': ['idempotency_key', 'expected_version', 'content', 'tokens', 'source_ids', 'parent_ids'],
  'history/compact': ['idempotency_key', 'expected_version', 'content', 'tokens', 'source_ids', 'parent_ids', 'from_ordinal', 'to_ordinal'],
  'draft/read': [],
  'draft/write': ['draft', 'expected_version', 'idempotency_key'],
  'artifact/list': ['content_type', 'filename', 'limit', 'cursor'],
  'artifact/create': ['idempotency_key', 'filename', 'content_type', 'content_sha256', 'content_base64', 'supersedes'],
} satisfies Record<string, string[]>;
type SessionOperation = keyof typeof sessionOperations;
const runOperations = {
  'installation/status': ['installation_id'],
  'installation/failure': ['installation_id'],
  'activity/work': ['from', 'to', 'timezone', 'page_size', 'last_key'],
  'memory/read': ['memory_id'],
  'memory/search': ['query', 'kinds', 'limit', 'cursor', 'labels'],
  'memory/write': ['memory_id', 'expected_version', 'idempotency_key', 'content', 'kind', 'tags', 'purpose', 'labels'],
  // First-party platform tools the owner is entitled to and has consented to (#8113).
  'tools/list': [],
  'tools/call': ['tool', 'arguments', 'request_id'],
} satisfies Record<string, string[]>;
type RunOperation = keyof typeof runOperations;
type ErrorCode = 'unavailable' | 'denied' | 'missing' | 'conflict' | 'expired' | 'incomplete' | 'invalid_response' | 'invalid_request' | 'scope_mismatch' | 'rate_limited';
const artifactId = z.string().regex(/^art_[a-f0-9]{12,64}$/);

/** Wait applied when a 429 carries no usable Retry-After (DATA04). */
export const DEFAULT_RETRY_AFTER_MS = 1_000;
/** Longest Retry-After the client records; larger server hints are clamped, not trusted verbatim. */
export const MAX_RETRY_AFTER_MS = 60_000;
/** Longest single wait before the one bounded rate-limit retry. */
export const RATE_LIMIT_WAIT_CAP_MS = 5_000;

/** Retry-After is either delta-seconds or an HTTP-date (RFC 9110 §10.2.3). */
export function parseRetryAfterMs(header: string | null, now: number = Date.now()): number {
  const value = header?.trim() ?? '';
  let ms: number | undefined;
  if (/^\d{1,9}$/.test(value)) ms = Number(value) * 1_000;
  else if (/^[A-Za-z]{3}, \d/.test(value)) {
    // IMF-fixdate only ("Sun, 06 Nov 1994 08:49:37 GMT"); Date.parse would also
    // accept bare numbers such as "-5" as years, which must fall to the default.
    const at = Date.parse(value);
    if (Number.isFinite(at)) ms = Math.max(0, at - now);
  }
  if (ms === undefined || !Number.isFinite(ms)) return DEFAULT_RETRY_AFTER_MS;
  return Math.min(ms, MAX_RETRY_AFTER_MS);
}

/** 401 bodies that mean "your bearer capability, not your scope" — refreshable once. */
const capabilityReasonSchema = z.enum(['capability_expired', 'capability_invalid']);
const capabilityErrorSchema = z.union([
  z.object({ error: capabilityReasonSchema }).strict(),
  z.object({ detail: z.object({ error: capabilityReasonSchema }).strict() }).strict(),
]);
type CapabilityReason = z.infer<typeof capabilityReasonSchema>;
/**
 * 503 body the gateway returns when the live model-policy decision for this turn was refused
 * (`probing_disabled`, `evidence_stale`, `not_permitted`, ...) or could not be established.
 * `retryable` is true only for a transient outage, never for a policy refusal (#7968).
 */
const modelPolicyRefusalSchema = z.object({
  error: z.literal('chat_model_policy_unavailable'),
  reason: z.string().regex(/^[a-z0-9_]{1,64}$/),
  retryable: z.boolean(),
  persona: z.string().max(128).nullable().optional(),
  model_id: z.string().max(256).nullable().optional(),
}).strict();
const modelPolicyErrorSchema = z.object({ detail: modelPolicyRefusalSchema }).strict();
export type ModelPolicyRefusal = z.infer<typeof modelPolicyRefusalSchema>;
/**
 * Any other 503 envelope the gateway names (`{"detail": {"error": "chat_memory_unconfigured"}}`
 * from FastAPI's HTTPException, or a bare `{"error": ...}`). Only the code is kept; the
 * rest of the body is never surfaced.
 */
const namedErrorBodySchema = z.union([z.object({ detail: z.object({ error: policyReason }) }), z.object({ error: policyReason })]);
/** Gateway answers that mean an optional chat store is simply not configured, not failing (#8028). */
export const UNCONFIGURED_STORE_ERRORS = ['chat_memory_unconfigured', 'chat_artifacts_unconfigured'] as const;
export type UnconfiguredStoreError = typeof UNCONFIGURED_STORE_ERRORS[number];
interface Classified { code: ErrorCode; retryAfterMs?: number; reason?: CapabilityReason; policy?: ModelPolicyRefusal; detail?: string }

async function responseErrorCode(response: Response): Promise<Classified> {
  // 429 is a throttle, 5xx is a source outage; neither is a caller mistake, and
  // neither may be flattened into invalid_request or an empty success (DATA04).
  if (response.status === 429) {
    await response.body?.cancel().catch(() => undefined);
    return { code: 'rate_limited', retryAfterMs: parseRetryAfterMs(response.headers.get('retry-after')) };
  }
  const fallback: ErrorCode = response.status === 409 ? 'conflict'
    : [401, 403, 404].includes(response.status) ? 'denied'
    : response.status === 410 ? 'expired'
    : [500, 502, 503, 504].includes(response.status) ? 'unavailable'
    : 'invalid_request';
  if (response.status === 503) {
    const body = await boundedJsonBody(response);
    const refusal = modelPolicyErrorSchema.safeParse(body);
    if (refusal.success) return { code: fallback, policy: refusal.data.detail };
    const named = namedErrorBodySchema.safeParse(body);
    return named.success ? { code: fallback, detail: 'detail' in named.data ? named.data.detail.error : named.data.error } : { code: fallback };
  }
  if (response.status !== 404 && response.status !== 401 && response.status !== 403) {
    await response.body?.cancel().catch(() => undefined);
    return { code: fallback };
  }
  const body = await boundedJsonBody(response);
  if (response.status === 403) {
    // A refused platform tool names its reason (`tool_not_consented`, ...) so the model can
    // tell the owner what to enable instead of guessing (#8113).
    const named = namedErrorBodySchema.safeParse(body);
    return named.success ? { code: fallback, detail: 'detail' in named.data ? named.data.detail.error : named.data.error } : { code: fallback };
  }
  if (response.status === 404) {
    return { code: body !== undefined && missingArtifactErrorSchema.safeParse(body).success ? 'missing' : fallback };
  }
  const capability = body === undefined ? undefined : capabilityErrorSchema.safeParse(body);
  return capability?.success ? { code: 'denied', reason: 'detail' in capability.data ? capability.data.detail.error : capability.data.error } : { code: fallback };
}

/** Read a small JSON error envelope; anything non-JSON, oversized or unreadable is `undefined`. */
async function boundedJsonBody(response: Response): Promise<unknown> {
  if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json' || !response.body) {
    await response.body?.cancel().catch(() => undefined);
    return undefined;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 4096) return undefined;
      chunks.push(value);
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    return undefined;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export class ChatDataError extends Error {
  constructor(
    readonly code: ErrorCode,
    readonly status?: number,
    readonly retryAfterMs?: number,
    /** Set only on a 401 the gateway attributes to the bearer capability itself. */
    readonly reason?: CapabilityReason,
    /** Set only on a 503 whose body names a refused or unavailable model-policy decision. */
    readonly policy?: ModelPolicyRefusal,
    /** The gateway's own error code from any other named 503 body (`chat_memory_unconfigured`, ...). */
    readonly detail?: string,
    /** Set only on a `denied` / `incomplete` from a rejected or unknown model-invoke receipt (#8048). */
    readonly refusal?: ModelInvokeRefusal,
  ) {
    super(`Chat data ${code}${status === undefined ? '' : ` (HTTP ${status})`}`);
    this.name = 'ChatDataError';
  }
}

/**
 * True when `error` is the gateway saying the named optional store has no backing table or
 * bucket (503 + that code). Callers degrade on this and on nothing else: any other 503, or the
 * same code on another status, keeps failing the operation as before (#8028).
 */
export function isStoreUnconfigured(error: unknown, store: UnconfiguredStoreError): error is ChatDataError {
  return error instanceof ChatDataError && error.status === 503 && error.code === 'unavailable' && error.detail === store;
}

export interface ChatDataClientConfig {
  baseUrl: string;
  workloadToken: () => Promise<string>;
  allowHttp?: boolean;
  crewRunId?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Wait primitive used before the bounded rate-limit retry; injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

export class ChatDataClient {
  get crewRunId() { return this.#crewRunId; }
  /**
   * Optional streaming twin of {@link invokeModel}, implemented by the Dev Box host client
   * (crew-host-model-client.ts) over the gateway's `invoke-with-response-stream` route. The chat
   * sandbox has no streaming route, so the loopback adapter synthesises events from the buffered
   * result when this is absent. Yields the gateway's Anthropic-format events in order.
   */
  declare invokeModelStream?: (operationId: string, input: ChatModelRequest, signal?: AbortSignal) => AsyncIterable<ModelStreamEvent>;
  readonly #origin: string;
  readonly #workloadToken: () => Promise<string>;
  readonly #crewRunId?: string;
  readonly #timeoutMs: number;
  readonly #sleep: (ms: number) => Promise<void>;
  readonly #signal?: AbortSignal;
  #binding?: Binding;
  #exchange?: Promise<Binding>;

  constructor(config: ChatDataClientConfig) {
    try {
      const parsed = new URL(config.baseUrl);
      if (parsed.pathname !== '/' || parsed.search || parsed.hash) throw new Error();
      this.#origin = validateBaseUrl(config.baseUrl, { allowHttp: config.allowHttp });
    } catch {
      throw new ChatDataError('invalid_request');
    }
    if (config.crewRunId !== undefined && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(config.crewRunId)) {
      throw new ChatDataError('invalid_request');
    }
    this.#crewRunId = config.crewRunId;
    this.#workloadToken = config.workloadToken;
    this.#signal = config.signal;
    this.#timeoutMs = config.timeoutMs ?? 15_000;
    if (!Number.isSafeInteger(this.#timeoutMs) || this.#timeoutMs < 1 || this.#timeoutMs > 60_000) {
      throw new ChatDataError('invalid_request');
    }
    this.#sleep = config.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  }

  async #request(path: string, body: string | undefined, headers: Record<string, string>, binary = false, channel: 'data' | 'model' | 'turn' = 'data',
    modelStream?: { binding: ModelStreamBinding; onText?: ModelTextListener }, operationSignal?: AbortSignal): Promise<unknown> {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, ...(this.#signal ? [this.#signal] : []), ...(operationSignal ? [operationSignal] : [])]);
    const timer = setTimeout(() => controller.abort(), channel === 'model' && path === 'invoke' ? 150_000 :
      path === 'session/next' ? Math.max(this.#timeoutMs, 25_000) : this.#timeoutMs);
    try {
      signal.throwIfAborted();
      const boundHeaders = channel === 'data' && path !== 'bootstrap'
        ? { ...headers, 'X-Adp-Workload-Token': await this.#readWorkloadToken() } : headers;
      // Origin comes from protected ADP_CHAT_DATA_URL bootstrap, validated at construction.
      // Private operation paths follow a fixed /v1/chat/ prefix; redirects are refused.
      // nosemgrep: tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf
      const response = await fetch(`${this.#origin}/v1/chat/${channel}/${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: body === undefined ? boundHeaders : { 'Content-Type': 'application/json', ...boundHeaders },
        body,
        redirect: 'error',
        signal,
      });
      if (!response.ok) {
        const classified = await responseErrorCode(response);
        throw new ChatDataError(classified.code, response.status, classified.retryAfterMs, classified.reason, classified.policy, classified.detail);
      }
      if (modelStream && response.headers.get('content-type')?.split(';')[0].trim() === MODEL_STREAM_TYPE && response.body) {
        try {
          return await readModelStream(response.body, modelStream.binding, signal, modelStream.onText);
        } catch (error) {
          throw new ChatDataError(error instanceof ModelStreamError ? error.code : 'incomplete');
        }
      }
      if ((!binary && response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') || !response.body) {
        await response.body?.cancel();
        throw new ChatDataError('invalid_response');
      }
      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > (channel === 'model' ? 65536 : (binary ? 8 : 2) * 1024 * 1024)) {
          await reader.cancel();
          throw new ChatDataError('invalid_response');
        }
        chunks.push(value);
      }
      const content = Buffer.concat(chunks);
      signal.throwIfAborted();
      if (binary) {
        const declaredLength = response.headers.get('content-length');
        if (!size || (declaredLength !== null && (!/^\d+$/.test(declaredLength) || Number(declaredLength) !== size))
          || !['clean', 'not_scanned'].includes(response.headers.get('x-artifact-scan-status') ?? '')) {
          throw new ChatDataError('invalid_response');
        }
        return content;
      }
      try {
        return JSON.parse(content.toString('utf8'));
      } catch {
        throw new ChatDataError('invalid_response');
      }
    } catch (error) {
      if (error instanceof ChatDataError) throw error;
      throw new ChatDataError('unavailable');
    } finally {
      clearTimeout(timer);
    }
  }

  async #readWorkloadToken(): Promise<string> {
    try {
      const token = (await this.#workloadToken()).trim();
      if (!token || token.length > 16_384 || !/^[A-Za-z0-9_.-]+$/.test(token)) throw new Error();
      return token;
    } catch {
      throw new ChatDataError('unavailable');
    }
  }

  async #bootstrap(): Promise<Binding> {
    const workloadToken = await this.#readWorkloadToken();
    let exchanged: unknown;
    try {
      exchanged = await this.#request('bootstrap', '{}', { 'X-Adp-Workload-Token': workloadToken });
    } catch (error) {
      // A refused exchange is final: there is no older capability to refresh, so
      // a capability-shaped 401 here must not re-enter the refresh path.
      if (error instanceof ChatDataError && error.code === 'denied') throw new ChatDataError('denied', error.status);
      throw error;
    }
    const parsed = bootstrapSchema.safeParse(exchanged);
    if (!parsed.success || parsed.data.expires_at <= Date.now() / 1000 || parsed.data.expires_at > Date.now() / 1000 + 300) {
      throw new ChatDataError('invalid_response');
    }
    const binding = parsed.data;
    if (this.#binding && (this.#binding.run_id !== binding.run_id || this.#binding.session_id !== binding.session_id ||
        (this.#binding.lease_generation !== undefined && this.#binding.lease_generation !== binding.lease_generation) ||
        (this.#binding.attempt !== undefined && this.#binding.attempt !== binding.attempt) ||
        this.#binding.session_mode !== binding.session_mode || this.#binding.qualification_required !== binding.qualification_required)) {
      throw new ChatDataError('scope_mismatch');
    }
    this.#binding = binding;
    return binding;
  }

  async #authorize(): Promise<Binding> {
    if (this.#binding && this.#binding.expires_at > Date.now() / 1000 + 30) return this.#binding;
    if (!this.#exchange) this.#exchange = this.#bootstrap().finally(() => { this.#exchange = undefined; });
    return this.#exchange;
  }

  /**
   * One bounded retry for transient classes only. `unavailable` retries at once;
   * `rate_limited` waits min(Retry-After, cap) first; a 401 the gateway attributes
   * to the capability (`capability_expired`/`capability_invalid`) refreshes the
   * capability once and retries. Every action re-serializes the same captured
   * payload, so a retried write carries identical bytes and the same idempotency
   * key. Other denied/conflict/expired/invalid_request never retry, and a second
   * failure after a refresh surfaces as plain `denied`.
   */
  async #withBinding<Result>(action: (binding: Binding) => Promise<Result>): Promise<Result> {
    let refreshed = false;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const binding = await this.#authorize();
        return await action(binding);
      } catch (error) {
        if (!(error instanceof ChatDataError)) throw error;
        if (attempt === 1) throw refreshed && error.code === 'denied' ? new ChatDataError('denied', error.status) : error;
        if (error.code === 'rate_limited') {
          await this.#sleep(Math.min(error.retryAfterMs ?? DEFAULT_RETRY_AFTER_MS, RATE_LIMIT_WAIT_CAP_MS));
        } else if (error.code === 'denied' && error.reason) {
          // Keep run/session so a changed binding on re-exchange is still refused;
          // only the cached capability is dropped.
          if (this.#binding) this.#binding = { ...this.#binding, expires_at: 0 };
          refreshed = true;
        } else if (error.code !== 'unavailable') {
          throw error;
        }
      }
    }
    throw new ChatDataError('unavailable');
  }

  async #withSession<Result>(sessionId: string, action: (binding: Binding) => Promise<Result>): Promise<Result> {
    if (!identifier.safeParse(sessionId).success) throw new ChatDataError('invalid_request');
    return this.#withBinding(binding => {
      if (binding.session_id !== sessionId) throw new ChatDataError('scope_mismatch');
      return action(binding);
    });
  }

  async runRequest(operation: RunOperation, payload: Record<string, unknown> = {}): Promise<unknown> {
    if (!Object.hasOwn(runOperations, operation)) throw new ChatDataError('invalid_request');
    const allowed: readonly string[] = runOperations[operation];
    if (Object.keys(payload).some(key => !allowed.includes(key))) throw new ChatDataError('invalid_request');
    const snapshot = JSON.stringify(payload);
    return this.#withBinding(binding => this.#request(
      operation, JSON.stringify({ ...JSON.parse(snapshot), run_id: binding.run_id }), { Authorization: `Bearer ${binding.capability}` },
    ));
  }

  async nextTurn(): Promise<z.infer<typeof acceptedTurnSchema>['turn'] & { session_sequence?: number; system_prompt?: string; prompt_revision?: string }> {
    return this.#withBinding(async binding => {
      if (!binding.lease_generation || !binding.attempt) throw new ChatDataError('invalid_response');
      const raw = await this.#request('next', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id,
      }), {
        Authorization: `Bearer ${binding.capability}`,
        'X-Adp-Workload-Token': await this.#readWorkloadToken(),
      }, false, 'turn');
      const parsed = acceptedTurnSchema.safeParse(raw);
      if (!parsed.success || parsed.data.run_id !== binding.run_id ||
        parsed.data.session_id !== binding.session_id ||
        parsed.data.lease_generation !== binding.lease_generation ||
        parsed.data.turn.ref !== `user_${createHash('sha256').update(binding.run_id).digest('hex')}` ||
        (binding.session_mode === 'persistent') !== (parsed.data.session_sequence !== undefined) ||
        (parsed.data.session_sequence !== undefined && parsed.data.session_sequence > 99_999_999)) {
        throw new ChatDataError('invalid_response');
      }
      return { ...parsed.data.turn, ...(parsed.data.session_sequence !== undefined
        ? { session_sequence: parsed.data.session_sequence } : {}),
      ...(parsed.data.system_prompt !== undefined && parsed.data.prompt_revision !== undefined
        ? { system_prompt: parsed.data.system_prompt, prompt_revision: parsed.data.prompt_revision } : {}) };
    });
  }

  async submitTurnResult(result: z.input<typeof turnResultSchema>): Promise<void> {
    const parsed = turnResultSchema.safeParse(result);
    if (!parsed.success) throw new ChatDataError('invalid_request');
    await this.#withBinding(async binding => {
      const raw = await this.#request('turn/result', JSON.stringify({
        ...parsed.data, run_id: binding.run_id, session_id: binding.session_id,
      }), { Authorization: `Bearer ${binding.capability}` });
      const receipt = z.object({
        run_id: identifier, session_id: identifier, attempt: z.number().int().positive(),
        lease_generation: z.number().int().positive(), sandbox_uid: identifier,
        outcome: z.enum(['completed', 'failed']), message_id: identifier.nullable(), terminal: z.literal(false),
        error_class: z.enum(['executor_not_wired', 'executor_down']).optional(),
        error_message: turnErrorMessage.optional(),
        reply_granularity: z.literal('row').optional(),
        evidence: answerEvidenceSchema.optional(),
      }).strict().safeParse(raw);
      const submittedEvidence = parsed.data.outcome === 'completed' ? parsed.data.evidence : undefined;
      if (!receipt.success || receipt.data.run_id !== binding.run_id || receipt.data.session_id !== binding.session_id ||
          receipt.data.attempt !== binding.attempt || receipt.data.lease_generation !== binding.lease_generation ||
          receipt.data.outcome !== parsed.data.outcome ||
          canonicalJson(receipt.data.evidence ?? null) !== canonicalJson(submittedEvidence ?? null) ||
          receipt.data.error_class !== (parsed.data.outcome === 'failed' ? parsed.data.error_class : undefined) ||
          receipt.data.error_message !== (parsed.data.outcome === 'failed' ? parsed.data.error_message : undefined) ||
          receipt.data.reply_granularity !== (parsed.data.outcome === 'completed' ? parsed.data.reply_granularity : undefined) ||
          receipt.data.message_id !== (parsed.data.outcome === 'completed' ? parsed.data.message_id : null)) {
        throw new ChatDataError('invalid_response');
      }
    });
  }

  async modelDecision(signal?: AbortSignal): Promise<{
    modelId: string; runId: string; tenantId: string; userId: string; generation: number;
    /** The chat model-policy posture the gateway issued this decision under (#7997). */
    posture: ModelPolicyPosture;
    /** False for an advisory/off decision issued despite a rejected or unverified check. */
    postureVerified: boolean;
    /** The rejected check an advisory/off decision was issued despite, when there was one. */
    rejectionReason?: string;
  }> {
    return this.#withBinding(async binding => {
      if (!binding.lease_generation || !binding.attempt) throw new ChatDataError('invalid_response');
      const nonce = randomBytes(32).toString('hex');
      const headers = {
        Authorization: `Bearer ${binding.capability}`,
        'X-Adp-Workload-Token': await this.#readWorkloadToken(),
      };
      const raw = await this.#request('decision', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id, nonce, model_policy_contract: 1,
      }), headers, false, 'model', undefined, signal);
      const reply = modelReplySchema.safeParse(raw);
      if (!reply.success) throw new ChatDataError('invalid_response');
      const keysRaw = await this.#request('keys', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id,
      }), headers, false, 'model', undefined, signal);
      const keys = modelKeysSchema.safeParse(keysRaw);
      if (!keys.success) throw new ChatDataError('invalid_response');
      const { result, assertion } = reply.data;
      const verified = verifyEnvelope(assertion, parseVerificationKeys(JSON.stringify(keys.data.keys)), {
        runId: binding.run_id, generation: binding.attempt,
        action: 'model_policy_response', commandId: nonce,
        audience: MODEL_POLICY_AUDIENCE, body: policyBody(result),
      });
      if (!verified.ok || verified.envelope.tenantId !== result.tenant_id ||
          verified.envelope.principal !== `${binding.run_id}#${binding.attempt}` ||
          result.invocation_id !== binding.run_id || result.attempt !== binding.attempt ||
          result.context.lease_generation !== binding.lease_generation || result.nonce !== nonce ||
          result.model_policy.decision.invocation_id !== binding.run_id ||
          result.model_policy.decision.tenant_id !== result.tenant_id) {
        throw new ChatDataError('denied');
      }
      const policy = result.model_policy;
      return { modelId: policy.decision.resolved_model_id, runId: binding.run_id,
        tenantId: result.tenant_id, userId: policy.decision.principal_id, generation: binding.lease_generation,
        posture: policy.posture, postureVerified: policy.posture_verified,
        ...(policy.posture !== 'enforcing' && policy.rejection_reason ? { rejectionReason: policy.rejection_reason } : {}) };
    });
  }

  async dashboardVerificationKeys(): Promise<Map<string, import('node:crypto').KeyObject>> {
    return this.#withBinding(async binding => {
      if (binding.session_mode !== 'persistent' || !binding.lease_generation) throw new ChatDataError('scope_mismatch');
      const raw = await this.#request('keys', JSON.stringify({ run_id: binding.run_id, session_id: binding.session_id }), {
        Authorization: `Bearer ${binding.capability}`,
        'X-Adp-Workload-Token': await this.#readWorkloadToken(),
      }, false, 'model');
      const parsed = modelKeysSchema.safeParse(raw);
      if (!parsed.success) throw new ChatDataError('invalid_response');
      return parseVerificationKeys(JSON.stringify(parsed.data.keys));
    });
  }

  async sessionScope(): Promise<{ run_id: string; session_id: string }> {
    return this.#withBinding(binding => Promise.resolve({ run_id: binding.run_id, session_id: binding.session_id }));
  }

  qualificationRequired(): boolean {
    return this.#binding?.qualification_required === true;
  }

  withSignal(signal: AbortSignal): ChatDataClient {
    const bounded = AbortSignal.any([signal, ...(this.#signal ? [this.#signal] : [])]);
    const client = new ChatDataClient({
      baseUrl: this.#origin, allowHttp: this.#origin.startsWith('http:'),
      workloadToken: this.#workloadToken, crewRunId: this.#crewRunId, timeoutMs: this.#timeoutMs,
      signal: bounded, sleep: async ms => { await delay(ms, undefined, { signal: bounded }); },
    });
    client.#binding = this.#binding;
    return client;
  }

  async qualificationRequest(operation: 'pending' | 'claim' | 'active' | 'result' | 'fixtures', input: unknown = {}, signal?: AbortSignal): Promise<unknown> {
    if (!['pending', 'claim', 'active', 'result', 'fixtures'].includes(operation)) throw new ChatDataError('invalid_request');
    const schema = operation === 'pending' ? z.object({}).strict() : operation === 'result' ? qualificationResult : qualificationClaim;
    const payload = schema.safeParse(input);
    if (!payload.success) throw new ChatDataError('invalid_request');
    return this.#withBinding(async binding => {
      if (!binding.qualification_required || binding.session_mode !== 'ephemeral') throw new ChatDataError('scope_mismatch');
      const raw = await this.#request(`qualification/${operation}`, JSON.stringify({
        ...payload.data, run_id: binding.run_id, session_id: binding.session_id,
      }), { Authorization: `Bearer ${binding.capability}` }, false, 'data', undefined, signal);
      if (operation === 'pending' || operation === 'claim') {
        const parsed = qualificationPlan.safeParse(raw);
        if (!parsed.success || parsed.data.binding.run_id !== binding.run_id || parsed.data.binding.session_id !== binding.session_id ||
            parsed.data.binding.attempt !== binding.attempt || parsed.data.binding.lease_generation !== binding.lease_generation) {
          throw new ChatDataError('scope_mismatch');
        }
        return parsed.data;
      }
      return operation === 'fixtures' ? qualificationFixtures.parse(raw) : raw;
    });
  }

  async qualificationDataProbe(probe: string, fixtures: QualificationFixtures, signal: AbortSignal): Promise<void> {
    const target = qualificationFixtures.parse(fixtures);
    await this.#withBinding(async binding => {
      if (!binding.qualification_required) throw new ChatDataError('scope_mismatch');
      const owner = target.owner;
      const headers: Record<string, string> = { Authorization: `Bearer ${binding.capability}` };
      const body: Record<string, unknown> = { run_id: binding.run_id, session_id: owner.session_id };
      let operation: string;
      if (['object_read', 'direct_url_read'].includes(probe)) {
        const run = probe === 'direct_url_read' ? owner.run_id : binding.run_id;
        await this.#request(`artifact/${encodeURIComponent(owner.session_id)}/${target.artifact_id}?run_id=${encodeURIComponent(run)}`,
          undefined, headers, true, 'data', undefined, signal);
        return;
      }
      const content = 'E45 synthetic isolation canary';
      const write = { idempotency_key: `e45-cross-${createHash('sha256').update(`${binding.run_id}:${probe}`).digest('hex')}`, content, tokens: 8, expected_version: target.history_version };
      switch (probe) {
        case 'session_read': operation = 'history/read'; body.limit = 1; break;
        case 'session_write': operation = 'draft/write'; Object.assign(body, { idempotency_key: write.idempotency_key, expected_version: 1, draft: { intent: content } }); break;
        case 'message_read': operation = 'history/messages'; body.ids = [target.message_id]; break;
        case 'message_write': operation = 'history/append'; Object.assign(body, write, { user_turn_id: `user_${createHash('sha256').update(owner.run_id).digest('hex')}` }); break;
        case 'summary_read': operation = 'history/summary'; body.summary_id = target.summary_id; break;
        case 'summary_write': operation = 'history/summary/append'; Object.assign(body, write, { source_ids: [target.message_id], parent_ids: [] }); break;
        case 'artifact_read': operation = 'artifact/list'; body.limit = 1; break;
        case 'artifact_write': operation = 'artifact/create'; Object.assign(body, { idempotency_key: write.idempotency_key, filename: 'e45.txt', content_type: 'text/plain',
          content_sha256: createHash('sha256').update(content).digest('hex'), content_base64: Buffer.from(content).toString('base64') }); break;
        case 'memory_read': operation = 'memory/read'; delete body.session_id; body.memory_id = target.memory_id; break;
        case 'memory_write': operation = 'memory/write'; delete body.session_id;
          Object.assign(body, { memory_id: target.memory_id, expected_version: target.memory_version, idempotency_key: write.idempotency_key, content, kind: 'fact' }); break;
        case 'cursor_read': operation = 'history/read'; Object.assign(body, { cursor: target.cursor, limit: 1 }); break;
        case 'forged_principal': operation = 'history/read'; body.principal = owner.user_id; break;
        case 'forged_run': operation = 'history/read'; body.run_id = owner.run_id; break;
        case 'forged_session': operation = 'history/read'; body.session_id = owner.session_id; break;
        case 'forged_generation': operation = 'history/read'; body.lease_generation = owner.lease_generation; break;
        case 'forged_user_header': operation = 'history/read'; headers['X-User-Id'] = owner.user_id; break;
        case 'forged_tenant_header': operation = 'history/read'; headers['X-Tenant-Id'] = owner.tenant_id; break;
        default: throw new ChatDataError('invalid_request');
      }
      await this.#request(operation, JSON.stringify(body), headers, false, 'data', undefined, signal);
    });
  }

  async sessionState(): Promise<z.infer<typeof sessionStateSchema>> {
    return this.#withBinding(async binding => {
      const raw = await this.#request('session/state', JSON.stringify({ run_id: binding.run_id, session_id: binding.session_id }),
        { Authorization: `Bearer ${binding.capability}` });
      const parsed = sessionStateSchema.safeParse(raw);
      if (!parsed.success || parsed.data.mode !== binding.session_mode) throw new ChatDataError('scope_mismatch');
      return parsed.data;
    });
  }

  async nextMailboxTurn(after: number): Promise<z.infer<typeof sessionNextSchema>['turn']> {
    if (!Number.isSafeInteger(after) || after < 0 || after > 99_999_999) throw new ChatDataError('invalid_request');
    return this.#withBinding(async binding => {
      if (!binding.lease_generation || binding.session_mode !== 'persistent') throw new ChatDataError('scope_mismatch');
      const raw = await this.#request('session/next', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id, after,
      }), { Authorization: `Bearer ${binding.capability}` });
      const parsed = sessionNextSchema.safeParse(raw);
      if (!parsed.success || parsed.data.run_id !== binding.run_id || parsed.data.session_id !== binding.session_id ||
        parsed.data.lease_generation !== binding.lease_generation ||
        (parsed.data.turn !== null && parsed.data.turn.sequence !== after + 1)) throw new ChatDataError('invalid_response');
      return parsed.data.turn;
    });
  }

  async sessionMode(): Promise<'ephemeral' | 'persistent'> {
    return (await this.#authorize()).session_mode;
  }

  async admitMailboxTurn(turn: NonNullable<z.infer<typeof sessionNextSchema>['turn']>): Promise<void> {
    if (this.#exchange) await this.#exchange;
    const previous = await this.#authorize();
    if (previous.session_mode !== 'persistent' || !previous.lease_generation ||
        !Number.isSafeInteger(turn.sequence) || turn.sequence < 2 || !identifier.safeParse(turn.turn_id).success) {
      throw new ChatDataError('invalid_request');
    }
    const raw = await this.#request('session/admit', JSON.stringify({
      run_id: previous.run_id, session_id: previous.session_id, after: turn.sequence - 1,
    }), { Authorization: `Bearer ${previous.capability}` });
    const parsed = bootstrapSchema.safeParse(raw);
    if (!parsed.success || parsed.data.run_id !== turn.turn_id || parsed.data.session_id !== previous.session_id ||
        parsed.data.run_id === previous.run_id || parsed.data.session_mode !== 'persistent' || !parsed.data.attempt ||
        parsed.data.lease_generation !== previous.lease_generation + 1 || parsed.data.expires_at <= Date.now() / 1000 ||
        parsed.data.expires_at > Date.now() / 1000 + 300) throw new ChatDataError('scope_mismatch');
    this.#binding = parsed.data;
  }

  async renewSession(): Promise<{ run_id: string; session_id: string; session_mode: 'ephemeral' | 'persistent' }> {
    if (!this.#exchange) this.#exchange = this.#bootstrap().finally(() => { this.#exchange = undefined; });
    const binding = await this.#exchange;
    return { run_id: binding.run_id, session_id: binding.session_id, session_mode: binding.session_mode };
  }

  async invokeTextModel(operationId: string, input: TextModelRequest) {
    const request = textModelRequestSchema.safeParse(input);
    if (!identifier.safeParse(operationId).success || !request.success) throw new ChatDataError('invalid_request');
    const result = await this.invokeModel(operationId, request.data);
    if (result.stopReason === 'tool_use' || result.content.some(block => block.type !== 'text')) throw new ChatDataError('invalid_response');
    return { text: result.content.map(block => block.type === 'text' ? block.text : '').join('\n'),
      stopReason: result.stopReason, usage: result.usage, modelId: result.modelId };
  }

  async requestTools(method: 'GET' | 'POST', path: string, body?: unknown) {
    const binding = await this.#authorize();
    if (binding.session_mode !== 'persistent' || !this.#crewRunId) throw new ChatDataError('scope_mismatch');
    const payload = JSON.stringify({ run_id: binding.run_id, session_id: binding.session_id, method, path,
      ...(body === undefined ? {} : { body }) });
    if (Buffer.byteLength(payload) > 65_536) throw new ChatDataError('invalid_request');
    const raw = await this.#request('tools', payload, {
      Authorization: `Bearer ${binding.capability}`, 'x-agent-runid': this.#crewRunId,
    });
    const result = z.object({ status: z.number().int().min(200).max(599), body: z.unknown() }).strict().safeParse(raw);
    if (!result.success) throw new ChatDataError('invalid_response');
    return result.data;
  }

  async invokeModel(operationId: string, input: ChatModelRequest, onText?: ModelTextListener, deliverResponse = false, evidence?: AnswerEvidence) {
    const request = modelRequestSchema.safeParse(input);
    if (!identifier.safeParse(operationId).success || !request.success) throw new ChatDataError('invalid_request');
    // Evidence rides only a delivered invocation: the relay attaches it to the streamed reply (#8113).
    const reported = evidence === undefined ? undefined : answerEvidenceSchema.safeParse(evidence);
    if (reported !== undefined && (!reported.success || !deliverResponse)) throw new ChatDataError('invalid_request');
    const snapshot = canonicalJson(request.data);
    if (Buffer.byteLength(snapshot) > 65_536) throw new ChatDataError('invalid_request');
    const digest = createHash('sha256').update(snapshot).digest('hex');
    return this.#withBinding(async binding => {
      if (!binding.lease_generation || !binding.attempt) throw new ChatDataError('invalid_response');
      if ((binding.session_mode === 'persistent') !== (this.#crewRunId !== undefined)) throw new ChatDataError('scope_mismatch');
      const raw = await this.#request('invoke', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id, operation_id: operationId, request: JSON.parse(snapshot),
        ...(deliverResponse ? { deliver_response: true } : {}),
        ...(reported?.success ? { evidence: reported.data } : {}),
      }), {
        Authorization: `Bearer ${binding.capability}`, 'X-Adp-Workload-Token': await this.#readWorkloadToken(), Accept: MODEL_STREAM_TYPE,
        ...(this.#crewRunId ? { 'x-agent-runid': this.#crewRunId } : {}),
      }, false, 'model', { binding: { run_id: binding.run_id, session_id: binding.session_id,
        lease_generation: binding.lease_generation, operation_id: operationId, request_digest: digest }, onText });
      const parsed = modelReceiptSchema.safeParse(raw);
      if (!parsed.success) throw new ChatDataError('invalid_response');
      const receipt = parsed.data;
      if (receipt.run_id !== binding.run_id || receipt.session_id !== binding.session_id ||
        receipt.lease_generation !== binding.lease_generation || receipt.operation_id !== operationId || receipt.request_digest !== digest) {
        throw new ChatDataError('scope_mismatch');
      }
      if (receipt.status === 'rejected' || receipt.status === 'unknown') {
        const refusal: ModelInvokeRefusal = { status: receipt.status,
          ...(receipt.error_code === undefined ? {} : { error_code: receipt.error_code }),
          ...(receipt.error_class === undefined ? {} : { error_class: receipt.error_class }),
          ...(receipt.error_detail === undefined ? {} : { error_detail: receipt.error_detail }) };
        throw new ChatDataError(receipt.status === 'rejected' ? 'denied' : 'incomplete', undefined, undefined, undefined, undefined, undefined, refusal);
      }
      if (receipt.status !== 'confirmed' || receipt.handoff !== 'confirmed' || receipt.reservation_status !== 'settled') {
        throw new ChatDataError('incomplete');
      }
      if (!receipt.content || !receipt.stop_reason || !receipt.usage || receipt.usage.output_tokens > request.data.max_tokens) {
        throw new ChatDataError('invalid_response');
      }
      const calls = receipt.content.filter(block => block.type === 'tool_use');
      if ((receipt.stop_reason === 'tool_use') !== (calls.length > 0) ||
        new Set(calls.map(call => call.id)).size !== calls.length ||
        calls.some(call => !request.data.tools?.some(tool => tool.name === call.name))) {
        throw new ChatDataError('invalid_response');
      }
      return { content: receipt.content, stopReason: receipt.stop_reason,
        usage: receipt.usage, modelId: receipt.model_id };
    });
  }

  async publishToolCall(toolCallId: string, name: string, args: unknown): Promise<void> {
    if (!identifier.safeParse(toolCallId).success || !identifier.safeParse(name).success) throw new ChatDataError('invalid_request');
    const argsBytes = Buffer.byteLength(JSON.stringify(args));
    const eventArgs = argsBytes <= 16_000 ? args : {
      _adp_tool_event: { arguments_omitted: true, utf8_bytes: argsBytes, limit_bytes: 16_000 },
    };
    await this.#withBinding(async binding => {
      if (binding.session_mode !== 'persistent' || !this.#crewRunId) throw new ChatDataError('scope_mismatch');
      await this.#request('tool-events', JSON.stringify({ run_id: binding.run_id, session_id: binding.session_id,
        operation_id: `tool_${toolCallId}`, tool_call_id: toolCallId, name, args: eventArgs }), {
        Authorization: `Bearer ${binding.capability}`, 'X-Adp-Workload-Token': await this.#readWorkloadToken(),
        'x-agent-runid': this.#crewRunId,
      }, false, 'model');
    });
  }

  async publishCrewTextEvent(operationId: string, sequence: number, eventType: 'start' | 'content' | 'end', delta = ''): Promise<void> {
    if (!/^crew_[a-f0-9]{64}$/.test(operationId) || !Number.isSafeInteger(sequence) || sequence < 0 || sequence > 16380 ||
        (eventType === 'content') !== Boolean(delta) || Buffer.byteLength(delta) > 4096) throw new ChatDataError('invalid_request');
    await this.#withBinding(async binding => {
      if (binding.session_mode !== 'persistent' || !this.#crewRunId) throw new ChatDataError('scope_mismatch');
      const raw = await this.#request('crew-events', JSON.stringify({ run_id: binding.run_id, session_id: binding.session_id,
        operation_id: operationId, sequence, event_type: eventType, delta }), {
        Authorization: `Bearer ${binding.capability}`, 'X-Adp-Workload-Token': await this.#readWorkloadToken(),
        'x-agent-runid': this.#crewRunId,
      }, false, 'model');
      if (!z.object({ accepted: z.literal(true) }).strict().safeParse(raw).success) throw new ChatDataError('invalid_response');
    });
  }

  async sessionRequest(operation: SessionOperation, sessionId: string, payload: Record<string, unknown> = {}): Promise<unknown> {
    if (!Object.hasOwn(sessionOperations, operation)) throw new ChatDataError('invalid_request');
    const allowed: readonly string[] = sessionOperations[operation];
    if (Object.keys(payload).some(key => !allowed.includes(key))) throw new ChatDataError('invalid_request');
    const snapshot = JSON.stringify(payload);
    return this.#withSession(sessionId, binding => {
      const body = JSON.stringify({ ...JSON.parse(snapshot), run_id: binding.run_id, session_id: binding.session_id });
      return this.#request(operation, body, { Authorization: `Bearer ${binding.capability}` });
    });
  }

  artifactUrl(sessionId: string, id: string): string {
    if (!artifactId.safeParse(id).success) throw new ChatDataError('invalid_request');
    if (!this.#binding || this.#binding.session_id !== sessionId) throw new ChatDataError('scope_mismatch');
    return `${this.#origin}/v1/chat/data/artifact/${encodeURIComponent(sessionId)}/${id}?run_id=${encodeURIComponent(this.#binding.run_id)}`;
  }

  async downloadArtifact(sessionId: string, id: string): Promise<Buffer> {
    if (!artifactId.safeParse(id).success) throw new ChatDataError('invalid_request');
    return this.#withSession(sessionId, async binding => {
      const path = `artifact/${encodeURIComponent(binding.session_id)}/${id}?run_id=${encodeURIComponent(binding.run_id)}`;
      const content = await this.#request(path, undefined, { Authorization: `Bearer ${binding.capability}` }, true);
      if (!Buffer.isBuffer(content)) throw new ChatDataError('invalid_response');
      return content;
    });
  }
}
