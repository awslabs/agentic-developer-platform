import { createHash, randomBytes } from 'node:crypto';
import { z } from 'zod';
import { MODEL_POLICY_AUDIENCE, parseVerificationKeys, verifyEnvelope } from '../../control-envelope';
import { policyBody } from '../../model-policy-body';
import { canonicalJson } from '../../invocability-probe/canonical-json';
import { validateBaseUrl } from '../../lib/url-guard';
import { ChatModelRequest, modelOutputBlock, modelRequestSchema } from './chat-model-contract';
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
}).strict();

const missingArtifactErrorSchema = z.object({
  detail: z.object({ error: z.literal('chat_artifact_missing') }).strict(),
}).strict();
const modelReplySchema = z.object({
  result: z.object({
    nonce: z.string().regex(/^[0-9a-f]{64}$/),
    invocation_id: identifier,
    tenant_id: identifier,
    attempt: z.number().int().positive(),
    context: z.object({ lease_generation: z.number().int().positive() }).passthrough(),
    model_policy: z.object({
      posture: z.literal('enforcing'),
      posture_verified: z.literal(true),
      status: z.literal('proposed'),
      decision: z.object({
        runtime_posture: z.literal('enforcing'),
        invocation_id: identifier,
        tenant_id: identifier,
        resolved_model_id: z.string().min(1),
      }).passthrough(),
    }).passthrough(),
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
}).strict();

export type TextModelRequest = z.infer<typeof textModelRequestSchema>;

const turnResultSchema = z.discriminatedUnion('outcome', [
  z.object({ outcome: z.literal('completed'), message_id: identifier }).strict(),
  z.object({ outcome: z.literal('failed') }).strict(),
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
interface Classified { code: ErrorCode; retryAfterMs?: number; reason?: CapabilityReason }

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
  if (response.status !== 404 && response.status !== 401) {
    await response.body?.cancel().catch(() => undefined);
    return { code: fallback };
  }
  const body = await boundedJsonBody(response);
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
  ) {
    super(`Chat data ${code}${status === undefined ? '' : ` (HTTP ${status})`}`);
    this.name = 'ChatDataError';
  }
}

export interface ChatDataClientConfig {
  baseUrl: string;
  workloadToken: () => Promise<string>;
  allowHttp?: boolean;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Wait primitive used before the bounded rate-limit retry; injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

export class ChatDataClient {
  readonly #origin: string;
  readonly #workloadToken: () => Promise<string>;
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
    this.#workloadToken = config.workloadToken;
    this.#signal = config.signal;
    this.#timeoutMs = config.timeoutMs ?? 15_000;
    if (!Number.isSafeInteger(this.#timeoutMs) || this.#timeoutMs < 1 || this.#timeoutMs > 60_000) {
      throw new ChatDataError('invalid_request');
    }
    this.#sleep = config.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  }

  async #request(path: string, body: string | undefined, headers: Record<string, string>, binary = false, channel: 'data' | 'model' | 'turn' = 'data',
    modelStream?: { binding: ModelStreamBinding; onText?: ModelTextListener }): Promise<unknown> {
    const controller = new AbortController();
    const signal = this.#signal ? AbortSignal.any([controller.signal, this.#signal]) : controller.signal;
    const timer = setTimeout(() => controller.abort(), channel === 'model' && path === 'invoke' ? 150_000 :
      path === 'session/next' ? Math.max(this.#timeoutMs, 25_000) : this.#timeoutMs);
    try {
      signal.throwIfAborted();
      const boundHeaders = channel === 'data' && path !== 'bootstrap'
        ? { ...headers, 'X-Adp-Workload-Token': await this.#readWorkloadToken() } : headers;
      const response = await fetch(`${this.#origin}/v1/chat/${channel}/${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: body === undefined ? boundHeaders : { 'Content-Type': 'application/json', ...boundHeaders },
        body,
        redirect: 'error',
        signal,
      });
      if (!response.ok) {
        const classified = await responseErrorCode(response);
        throw new ChatDataError(classified.code, response.status, classified.retryAfterMs, classified.reason);
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
        this.#binding.session_mode !== binding.session_mode)) {
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

  async nextTurn(): Promise<z.infer<typeof acceptedTurnSchema>['turn'] & { session_sequence?: number }> {
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
        ? { session_sequence: parsed.data.session_sequence } : {}) };
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
      }).strict().safeParse(raw);
      if (!receipt.success || receipt.data.run_id !== binding.run_id || receipt.data.session_id !== binding.session_id ||
          receipt.data.attempt !== binding.attempt || receipt.data.lease_generation !== binding.lease_generation ||
          receipt.data.outcome !== parsed.data.outcome ||
          receipt.data.message_id !== (parsed.data.outcome === 'completed' ? parsed.data.message_id : null)) {
        throw new ChatDataError('invalid_response');
      }
    });
  }

  async modelDecision(): Promise<{ modelId: string; runId: string; tenantId: string; generation: number }> {
    return this.#withBinding(async binding => {
      if (!binding.lease_generation || !binding.attempt) throw new ChatDataError('invalid_response');
      const nonce = randomBytes(32).toString('hex');
      const headers = {
        Authorization: `Bearer ${binding.capability}`,
        'X-Adp-Workload-Token': await this.#readWorkloadToken(),
      };
      const raw = await this.#request('decision', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id, nonce, model_policy_contract: 1,
      }), headers, false, 'model');
      const reply = modelReplySchema.safeParse(raw);
      if (!reply.success) throw new ChatDataError('invalid_response');
      const keysRaw = await this.#request('keys', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id,
      }), headers, false, 'model');
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
      return { modelId: result.model_policy.decision.resolved_model_id, runId: binding.run_id,
        tenantId: result.tenant_id, generation: binding.lease_generation };
    });
  }

  async sessionScope(): Promise<{ run_id: string; session_id: string }> {
    return this.#withBinding(binding => Promise.resolve({ run_id: binding.run_id, session_id: binding.session_id }));
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

  async invokeModel(operationId: string, input: ChatModelRequest, onText?: ModelTextListener, deliverResponse = false) {
    const request = modelRequestSchema.safeParse(input);
    if (!identifier.safeParse(operationId).success || !request.success) throw new ChatDataError('invalid_request');
    const snapshot = canonicalJson(request.data);
    if (Buffer.byteLength(snapshot) > 65_536) throw new ChatDataError('invalid_request');
    const digest = createHash('sha256').update(snapshot).digest('hex');
    return this.#withBinding(async binding => {
      if (!binding.lease_generation || !binding.attempt) throw new ChatDataError('invalid_response');
      const raw = await this.#request('invoke', JSON.stringify({
        run_id: binding.run_id, session_id: binding.session_id, operation_id: operationId, request: JSON.parse(snapshot),
        ...(deliverResponse ? { deliver_response: true } : {}),
      }), {
        Authorization: `Bearer ${binding.capability}`, 'X-Adp-Workload-Token': await this.#readWorkloadToken(), Accept: MODEL_STREAM_TYPE,
      }, false, 'model', { binding: { run_id: binding.run_id, session_id: binding.session_id,
        lease_generation: binding.lease_generation, operation_id: operationId, request_digest: digest }, onText });
      const parsed = modelReceiptSchema.safeParse(raw);
      if (!parsed.success) throw new ChatDataError('invalid_response');
      const receipt = parsed.data;
      if (receipt.run_id !== binding.run_id || receipt.session_id !== binding.session_id ||
        receipt.lease_generation !== binding.lease_generation || receipt.operation_id !== operationId || receipt.request_digest !== digest) {
        throw new ChatDataError('scope_mismatch');
      }
      if (receipt.status === 'rejected') throw new ChatDataError('denied');
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
