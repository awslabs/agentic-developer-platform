import { z } from 'zod';
import { validateBaseUrl } from '../../lib/url-guard';

const identifier = z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/);
const bootstrapSchema = z.object({
  capability: z.string().min(1).max(4096).regex(/^[A-Za-z0-9_.-]+$/),
  run_id: identifier,
  session_id: identifier,
  expires_at: z.number().int().positive(),
}).strict();
const missingArtifactErrorSchema = z.object({
  detail: z.object({ error: z.literal('chat_artifact_missing') }).strict(),
}).strict();

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
const capabilityErrorSchema = z.object({ error: z.enum(['capability_expired', 'capability_invalid']) }).strict();
type CapabilityReason = z.infer<typeof capabilityErrorSchema>['error'];
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
  return capability?.success ? { code: 'denied', reason: capability.data.error } : { code: fallback };
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
  /** Wait primitive used before the bounded rate-limit retry; injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

export class ChatDataClient {
  readonly #origin: string;
  readonly #workloadToken: () => Promise<string>;
  readonly #timeoutMs: number;
  readonly #sleep: (ms: number) => Promise<void>;
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
    this.#timeoutMs = config.timeoutMs ?? 15_000;
    if (!Number.isSafeInteger(this.#timeoutMs) || this.#timeoutMs < 1 || this.#timeoutMs > 60_000) {
      throw new ChatDataError('invalid_request');
    }
    this.#sleep = config.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  }

  async #request(path: string, body: string | undefined, headers: Record<string, string>, binary = false): Promise<unknown> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      const response = await fetch(`${this.#origin}/v1/chat/data/${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: body === undefined ? headers : { 'Content-Type': 'application/json', ...headers },
        body,
        redirect: 'error',
        signal: controller.signal,
      });
      if (!response.ok) {
        const classified = await responseErrorCode(response);
        throw new ChatDataError(classified.code, response.status, classified.retryAfterMs, classified.reason);
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
        if (size > (binary ? 8 : 2) * 1024 * 1024) {
          await reader.cancel();
          throw new ChatDataError('invalid_response');
        }
        chunks.push(value);
      }
      const content = Buffer.concat(chunks);
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

  async #bootstrap(): Promise<Binding> {
    let workloadToken: string;
    try {
      workloadToken = (await this.#workloadToken()).trim();
      if (!workloadToken || workloadToken.length > 16_384 || !/^[A-Za-z0-9_.-]+$/.test(workloadToken)) throw new Error();
    } catch {
      throw new ChatDataError('unavailable');
    }
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
    if (this.#binding && (this.#binding.run_id !== binding.run_id || this.#binding.session_id !== binding.session_id)) {
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

  async sessionScope(): Promise<{ run_id: string; session_id: string }> {
    return this.#withBinding(binding => Promise.resolve({ run_id: binding.run_id, session_id: binding.session_id }));
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
