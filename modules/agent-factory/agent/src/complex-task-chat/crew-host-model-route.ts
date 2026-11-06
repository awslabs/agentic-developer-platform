import { AsyncLocalStorage } from 'node:async_hooks';
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { z } from 'zod';

type Credentials = { accessKeyId: string; secretAccessKey: string; sessionToken?: string };

export class CrewHostModelRouteError extends Error {
  constructor(readonly status: number) { super(`Crew host model route refused (${status})`); }
}

export type CrewHostRouteOptions = {
  gatewayUrl: string; region: string; modelId: string; runId: string;
  currentRun: () => string;
  credentials: () => Promise<Credentials>;
  modelToken: () => Promise<string>;
  /** Unix seconds the delivered model token expires, when the host can read it (#8223 Dev Box tool latency). */
  modelTokenExpiresAt?: () => Promise<number | undefined>;
  send?: typeof fetch;
};

/**
 * A host call skips the bare token-refresh round trip while the delivered token is still good for
 * this long. Every tool step and every model call paid a refresh (~0.5 s through API Gateway)
 * although the gateway pushes a 60 s token on each refresh *and* on each accepted activity mark, so
 * steady-state model calls only ever needed their busy mark (#8223 Dev Box tool latency).
 */
export const TOOLS_TOKEN_REFRESH_MARGIN_SECONDS = 20;

/** True while the token the gateway last pushed is good for the margin; an unreadable expiry is not fresh. */
async function deliveredTokenIsFresh(options: CrewHostRouteOptions): Promise<boolean> {
  let expiresAt: number | undefined;
  // A reader that throws synchronously (an expired or missing token file read inside the call) is
  // "not fresh" too, exactly like one that rejects (#8223 cause 55); only the refresh may fail a call.
  try { expiresAt = await options.modelTokenExpiresAt?.(); } catch { return false; }
  return typeof expiresAt === 'number' && expiresAt - Math.floor(Date.now() / 1000) >= TOOLS_TOKEN_REFRESH_MARGIN_SECONDS;
}

/** The approved Regional API Gateway stage URL for this run, or a refusal; shared by every host route. */
function approvedHostRoute(options: CrewHostRouteOptions): URL {
  const url = new URL(options.gatewayUrl);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.port ||
      !/^[a-z0-9-]+$/.test(options.region) ||
      !new RegExp(`^[a-z0-9]+\\.execute-api\\.${options.region}\\.amazonaws\\.com$`).test(url.hostname) ||
      !/^\/[A-Za-z0-9_-]+\/?$/.test(url.pathname) ||
      !/^[A-Za-z0-9_.:-]{1,256}$/.test(options.modelId) ||
      !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(options.runId) ||
      options.currentRun() !== options.runId) {
    throw new Error('Approved Crew host model route unavailable');
  }
  return url;
}

export const TOOLS_REQUEST_MAX_BYTES = 1_048_576;

const toolsReplySchema = z.object({ status: z.number().int().min(200).max(599), body: z.unknown() }).strict();

/**
 * Relay one `adp mcp` request through the signed host tools route (#8380).
 *
 * Same proof as a model call: instance-role SigV4 plus the current gateway-minted model token,
 * refreshed first so the gateway has pushed a token for this run. The gateway answers with the
 * façade's status and body; a refusal of the host itself is a thrown route error.
 */
export async function invokeCrewHostTools(options: CrewHostRouteOptions, method: 'GET' | 'POST', path: string,
  body?: unknown, signal?: AbortSignal): Promise<{ status: number; body: unknown }> {
  return invokeCrewHostBoundRoute(options, method, path, body, signal, false);
}

export function allowedCrewOrchestrationPath(method: string, path: string): boolean {
  const [route, query, ...rest] = path.split('?');
  if (rest.length || route.includes('%') || path.includes('#') || path.includes('\\') || path.length > 2048 ||
      (query !== undefined && (!/^[A-Za-z0-9_.~+%=&-]*$/.test(query) || /%(?![A-Fa-f0-9]{2})/.test(query)))) return false;
  const source = /^\/orchestration\/products\/[A-Za-z0-9_-]+\/projects\/[A-Za-z0-9_-]+\/sources$/.test(route);
  // #9522: the adp-flow preflight reads the Product profile (delivery home) and the Project budget.
  const profile = /^\/orchestration\/products\/[A-Za-z0-9_-]+\/projects\/[A-Za-z0-9_-]+$/.test(route);
  const budget = /^\/orchestration\/projects\/[A-Za-z0-9_-]+\/budget$/.test(route);
  const flow = /^\/orchestration\/flows\/[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:\/(?:plans|decisions))?$/.test(route);
  // #9640: a verified host run exchanges its identity for the owner's session (the box CLI then
  // calls the public API as the laptop does); the gateway proves the run, this only relays.
  const parameters = method === 'POST' && route === '/orchestration/owner-session' ? [] :
    method === 'POST' && route === '/orchestration/flows/drafts' ? ['reason'] :
    method === 'POST' && route === '/orchestration/flows/drafts/preview' ? [] :
    method === 'GET' && route === '/orchestration/products' ? ['limit', 'cursor'] :
    method === 'GET' && source ? ['limit', 'cursor', 'connection_id', 'connection_kind', 'resource_cursor'] :
    method === 'GET' && route === '/orchestration/flows' ? ['limit', 'offset', 'status', 'include_archived', 'needs_me'] :
    method === 'GET' && (route === '/orchestration/github/status' || flow || profile || budget) ? [] : null;
  if (parameters === null) return false;
  if (query !== undefined) {
    const keys = [...new URLSearchParams(query).keys()];
    if (keys.length !== new Set(keys).size || keys.some(key => !parameters.includes(key))) return false;
  }
  return true;
}

export async function invokeCrewHostOrchestration(options: CrewHostRouteOptions, method: 'GET' | 'POST', path: string,
  body?: unknown, signal?: AbortSignal): Promise<{ status: number; body: unknown }> {
  if (!allowedCrewOrchestrationPath(method, path)) throw new Error('Crew orchestration route unavailable');
  return invokeCrewHostBoundRoute(options, method, path, body, signal, true);
}

async function invokeCrewHostBoundRoute(options: CrewHostRouteOptions, method: 'GET' | 'POST', path: string,
  body: unknown, signal: AbortSignal | undefined, orchestration: boolean): Promise<{ status: number; body: unknown }> {
  signal?.throwIfAborted();
  const url = approvedHostRoute(options);
  if ((!orchestration && (!/^\/tools(?:[/?]|$)/.test(path) || path.length > 2048)) || (method === 'GET' && body !== undefined)) {
    throw new Error('Crew tools route unavailable');
  }
  const credentials = await options.credentials();
  if (!credentials.sessionToken || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const signer = new SignatureV4({ credentials, region: options.region, service: 'execute-api',
    sha256: Hash.bind(null, 'sha256') });
  const stage = url.pathname.replace(/\/$/, '');
  if (!(await deliveredTokenIsFresh(options))) {
    const refreshPath = `${stage}/agent/crew/model-token/refresh`;
    const refresh = await signer.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname,
      path: refreshPath, body: '{}',
      headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId } });
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
    const delivered = await (options.send ?? fetch)(`${url.origin}${refreshPath}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15_000), headers: refresh.headers, body: '{}',
    });
    await delivered.body?.cancel();
    if (delivered.status !== 204) throw new CrewHostModelRouteError(delivered.status);
  }
  const token = await options.modelToken();
  if (!token || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token) || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const payload = orchestration ? (method === 'POST' ? JSON.stringify(body) : '') :
    JSON.stringify({ method, path, ...(body === undefined ? {} : { body }) });
  // 1 MiB: a tool call's arguments (a file body, a long query) must fit; the gateway relay and façade
  // accept this size and API Gateway allows 10 MB (#8223 cause 36).
  if (Buffer.byteLength(payload) > TOOLS_REQUEST_MAX_BYTES) throw new Error('Crew tools request too large');
  const toolsPath = orchestration ? `${stage}/agent/crew${path}` : `${stage}/agent/crew/tools`;
  const destination = new URL(toolsPath, url.origin);
  const signed = await signer.sign({ method: orchestration ? method : 'POST', protocol: 'https:', hostname: url.hostname,
    path: destination.pathname, query: Object.fromEntries(destination.searchParams), body: payload,
    headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId, 'x-adp-crew-model-token': token } });
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  const response = await (options.send ?? fetch)(`${url.origin}${toolsPath}`, {
    method: orchestration ? method : 'POST', redirect: 'error', signal: AbortSignal.any([AbortSignal.timeout(30_000), ...(signal ? [signal] : [])]),
    headers: signed.headers, ...(method === 'GET' && orchestration ? {} : { body: payload }),
  });
  if (!response.ok && !orchestration) {
    await response.body?.cancel();
    throw new CrewHostModelRouteError(response.status);
  }
  if (options.currentRun() !== options.runId) {
    await response.body?.cancel();
    throw new Error('Crew host run was fenced');
  }
  const reply = orchestration ? { status: response.status, body: await response.json() } : toolsReplySchema.parse(await response.json());
  signal?.throwIfAborted();
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  return reply;
}


const githubTokenReplySchema = z.object({
  token: z.string().min(1).max(16384).regex(/^\S+$/),
  login: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/),
  expires_at: z.number().int().positive(),
}).strict();

export type CrewGithubToken = z.infer<typeof githubTokenReplySchema>;

/**
 * The owner's GitHub token for this box's `gh` and `git` (#8555): same signed host proof as a
 * model call — instance-role SigV4 plus the current gateway-minted model token — against the
 * gateway's `/agent/crew/github-token`, which answers only for the owner's current host run and
 * only from the owner's consented GitHub connection. A refusal is a route error with the gateway's
 * status (404 not connected, 403 consent missing, 409 reconnect/fenced, 503 disabled).
 */
export async function invokeCrewHostGithubToken(options: CrewHostRouteOptions, signal?: AbortSignal): Promise<CrewGithubToken> {
  signal?.throwIfAborted();
  const url = approvedHostRoute(options);
  const credentials = await options.credentials();
  if (!credentials.sessionToken || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const signer = new SignatureV4({ credentials, region: options.region, service: 'execute-api',
    sha256: Hash.bind(null, 'sha256') });
  const stage = url.pathname.replace(/\/$/, '');
  if (!(await deliveredTokenIsFresh(options))) {
    const refreshPath = `${stage}/agent/crew/model-token/refresh`;
    const refresh = await signer.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname,
      path: refreshPath, body: '{}',
      headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId } });
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
    const delivered = await (options.send ?? fetch)(`${url.origin}${refreshPath}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15_000), headers: refresh.headers, body: '{}',
    });
    await delivered.body?.cancel();
    if (delivered.status !== 204) throw new CrewHostModelRouteError(delivered.status);
  }
  const token = await options.modelToken();
  if (!token || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token) || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const path = `${stage}/agent/crew/github-token`;
  const signed = await signer.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname, path, body: '',
    headers: { host: url.hostname, 'x-agent-runid': options.runId, 'x-adp-crew-model-token': token } });
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  const response = await (options.send ?? fetch)(`${url.origin}${path}`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.any([AbortSignal.timeout(30_000), ...(signal ? [signal] : [])]), headers: signed.headers,
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new CrewHostModelRouteError(response.status);
  }
  if (options.currentRun() !== options.runId) {
    await response.body?.cancel();
    throw new Error('Crew host run was fenced');
  }
  const reply = githubTokenReplySchema.parse(await response.json());
  signal?.throwIfAborted();
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  return reply;
}

export const awsRequestSchema = z.object({
  operation: z.enum(['list', 'assume']), connection_id: z.string().min(1).max(128).optional(),
  label: z.string().min(1).max(255).optional(),
}).strict();
const awsReplySchema = z.union([
  z.object({ connections: z.array(z.object({ id: z.string().min(1).max(128), label: z.string().max(255),
    account_id: z.string().regex(/^[0-9]{12}$/) }).strict()).max(1000) }).strict(),
  z.object({ Version: z.literal(1), AccessKeyId: z.string().min(1).max(256),
    SecretAccessKey: z.string().min(1).max(16384), SessionToken: z.string().min(1).max(32768),
    Expiration: z.string().datetime({ offset: true }) }).strict(),
]);

/** No credential cache: every SDK refresh rechecks live host and connection authority. */
export async function invokeCrewHostAwsCredentials(options: CrewHostRouteOptions, input: unknown, signal?: AbortSignal): Promise<unknown> {
  signal?.throwIfAborted();
  const payload = JSON.stringify(awsRequestSchema.parse(input));
  const url = approvedHostRoute(options);
  const credentials = await options.credentials();
  if (!credentials.sessionToken || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const signer = new SignatureV4({ credentials, region: options.region, service: 'execute-api',
    sha256: Hash.bind(null, 'sha256') });
  const stage = url.pathname.replace(/\/$/, '');
  if (!(await deliveredTokenIsFresh(options))) {
    const refreshPath = `${stage}/agent/crew/model-token/refresh`;
    const refresh = await signer.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname,
      path: refreshPath, body: '{}',
      headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId } });
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
    const delivered = await (options.send ?? fetch)(`${url.origin}${refreshPath}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15_000), headers: refresh.headers, body: '{}',
    });
    await delivered.body?.cancel();
    if (delivered.status !== 204) throw new CrewHostModelRouteError(delivered.status);
  }
  const token = await options.modelToken();
  if (!token || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token) || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const path = `${stage}/agent/crew/aws-credentials`;
  const signed = await signer.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname, path, body: payload,
    headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId, 'x-adp-crew-model-token': token } });
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  const response = await (options.send ?? fetch)(`${url.origin}${path}`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.any([AbortSignal.timeout(30_000), ...(signal ? [signal] : [])]), headers: signed.headers, body: payload,
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new CrewHostModelRouteError(response.status);
  }
  if (options.currentRun() !== options.runId) {
    await response.body?.cancel();
    throw new Error('Crew host run was fenced');
  }
  const reply = awsReplySchema.parse(await response.json());
  signal?.throwIfAborted();
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  if ('Expiration' in reply && Date.parse(reply.Expiration) <= Date.now()) throw new Error('Expired AWS session');
  return reply;
}


export type CrewHostResponsesReply = { status: number; contentType: string; body: ReadableStream<Uint8Array> | null };

/**
 * Forward a Codex Responses request untouched to the gateway's OpenAI passthrough (GPT models,
 * dev 2026-10-26). Same signed host proof and heartbeat as a model call; the reply (JSON or SSE)
 * is handed back as a byte stream for the adapter to relay verbatim. Upstream 4xx are returned,
 * not thrown, so Codex sees the gateway's own error body (model not allowed, invalid request).
 */
export async function invokeCrewHostResponses(options: CrewHostRouteOptions, body: unknown, signal?: AbortSignal):
  Promise<CrewHostResponsesReply & { finish: (completed: boolean) => Promise<void>; stopped: AbortController }> {
  signal?.throwIfAborted();
  const call = await beginHostModelCall(options, body, 'openai-responses', { accept: 'text/event-stream, application/json' });
  try {
    const response = await (options.send ?? fetch)(`${call.url.origin}${call.path}`, {
      method: 'POST', redirect: 'error', headers: call.headers, body: call.payload,
      signal: AbortSignal.any([AbortSignal.timeout(3_660_000), call.stopped.signal, ...(signal ? [signal] : [])]),
    });
    if (call.failure()) { await response.body?.cancel(); throw call.failure(); }
    if (response.status >= 500 || response.status === 401 || response.status === 403 || response.status === 409) {
      await response.body?.cancel();
      throw new CrewHostModelRouteError(response.status);
    }
    if (options.currentRun() !== options.runId) { await response.body?.cancel(); throw new Error('Crew host run was fenced'); }
    return { status: response.status, contentType: response.headers.get('content-type') ?? 'application/json',
      body: response.body as ReadableStream<Uint8Array> | null, finish: call.finish, stopped: call.stopped };
  } catch (error) {
    call.stopped.abort();
    await call.finish(false);
    throw call.failure() ?? error;
  }
}

/**
 * Run a pre-call (token refresh, busy mark) again once when it failed on transport or a 5xx: one
 * slow or dropped answer must not fail the owner's turn (#8223 cause 48). A refusal (4xx) or a
 * fence is final and is not retried.
 */
async function retryOnce<T>(call: () => Promise<T>): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (error instanceof CrewHostModelRouteError ? error.status < 500 : error instanceof Error && /fenced|unavailable/.test(error.message)) throw error;
    return await call();
  }
}

/** One parsed Server-Sent Event from the gateway's streaming model route. */
export type HostModelStreamEvent = { event: string; data: string };

type HostModelCall = {
  url: URL; path: string; headers: Record<string, string>; payload: string;
  stopped: AbortController; finish: (completed: boolean) => Promise<void>; failure: () => unknown;
};

/**
 * The prologue every host model call shares: approved route, fresh instance credentials, a token
 * refresh when the delivered token is not fresh (the busy mark below pushes a new one, so a call
 * within 40 s of the last pays only the mark), the busy activity mark, and the signed request.
 * Returns the signed call plus a 30 s heartbeat that keeps the token and the busy mark alive while
 * the call is in flight; a refused renewal aborts the call through `stopped`. `finish(completed)`
 * stops the heartbeat and, when the call completed under the same run, sends the idle mark without
 * holding the caller's reply.
 */
/** A model id the owner may name per call (Claude Code `/model`, Codex `-c model=`); the gateway decides entitlement. */
export const MODEL_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,256}$/;

/**
 * The harness behind the current loopback request (#9024).
 *
 * The adapter's own fetch reaches the gateway as `node`, so the gateway could not tell a Claude Code
 * session on the box from Codex or from Kiro Crew's own calls. The adapter runs each incoming request
 * inside this store with the client's User-Agent; every host call made while serving it carries that
 * value as `x-adp-client-user-agent`, which the gateway trusts only on the host route.
 */
export const hostCallClient = new AsyncLocalStorage<{ userAgent?: string; session?: string }>();
export const CLIENT_USER_AGENT_HEADER = 'x-adp-client-user-agent';
/** Claude Code's `metadata.user_id` (…`_session_<uuid>`): the adapter rebuilds the body for Bedrock, which has no `metadata`. */
export const CLIENT_SESSION_HEADER = 'x-adp-client-session';

/** Header-safe: printable ASCII only, bounded; undefined when nothing usable remains. */
export function clientUserAgentHeader(userAgent: string | string[] | undefined): string | undefined {
  const value = (Array.isArray(userAgent) ? userAgent[0] : userAgent ?? '').replace(/[^\x20-\x7e]+/g, ' ').trim().slice(0, 256);
  return value || undefined;
}

async function beginHostModelCall(options: CrewHostRouteOptions, body: unknown, route: 'invoke' | 'invoke-with-response-stream' | 'openai-responses',
  extraHeaders: Record<string, string> = {}, model?: string): Promise<HostModelCall> {
  const url = approvedHostRoute(options);
  // The owner may name another model per call; the gateway resolves it and applies their entitlement.
  if (model !== undefined && !MODEL_ID_PATTERN.test(model)) throw new Error('Approved Crew host model route unavailable');
  const modelId = model ?? options.modelId;
  const credentials = await options.credentials();
  if (!credentials.sessionToken || options.currentRun() !== options.runId) {
    throw new Error('Current Crew host identity unavailable');
  }
  const signer = new SignatureV4({ credentials, region: options.region, service: 'execute-api',
    sha256: Hash.bind(null, 'sha256') });
  const refreshPath = `${url.pathname.replace(/\/$/, '')}/agent/crew/model-token/refresh`;
  const refreshBody = '{}';
  const refresh = await signer.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname,
    path: refreshPath, body: refreshBody,
    headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId } });
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  if (!(await deliveredTokenIsFresh(options))) {
    await retryOnce(async () => {
      const delivered = await (options.send ?? fetch)(`${url.origin}${refreshPath}`, {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15_000), headers: refresh.headers, body: refreshBody,
      });
      await delivered.body?.cancel();
      if (delivered.status !== 204) throw new CrewHostModelRouteError(delivered.status);
    });
  }
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  const readToken = async () => {
    const token = await options.modelToken();
    if (!token || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token) || options.currentRun() !== options.runId) {
      throw new Error('Current Crew host identity unavailable');
    }
    return token;
  };
  const activity = async (busy: boolean) => {
    const freshCredentials = await options.credentials();
    if (!freshCredentials.sessionToken || options.currentRun() !== options.runId) {
      throw new Error('Current Crew host identity unavailable');
    }
    const activitySigner = new SignatureV4({ credentials: freshCredentials, region: options.region, service: 'execute-api',
      sha256: Hash.bind(null, 'sha256') });
    const payload = JSON.stringify({ busy, consolidation_complete: false });
    const signedActivity = await activitySigner.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname,
      path: refreshPath, body: payload,
      headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId,
        'x-adp-crew-model-token': await readToken() } });
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
    const reply = await (options.send ?? fetch)(`${url.origin}${refreshPath}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15_000), headers: signedActivity.headers, body: payload,
    });
    await reply.body?.cancel();
    if (reply.status !== 204) throw new CrewHostModelRouteError(reply.status);
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  };
  await retryOnce(() => activity(true));
  const modelToken = await readToken();
  // Codex on GPT models: the gateway's OpenAI passthrough, admitted for the host with the same proof.
  const path = route === 'openai-responses'
    ? `${url.pathname.replace(/\/$/, '')}/agent/openai/v1/responses`
    : `${url.pathname.replace(/\/$/, '')}/agent/model/${encodeURIComponent(modelId)}/${route}`;
  const payload = JSON.stringify(body);
  const signed = await signer.sign({
    method: 'POST', protocol: 'https:', hostname: url.hostname, path, body: payload,
    headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId,
      'x-adp-crew-model-token': modelToken,
      ...(hostCallClient.getStore()?.userAgent ? { [CLIENT_USER_AGENT_HEADER]: hostCallClient.getStore()!.userAgent! } : {}),
      ...(hostCallClient.getStore()?.session ? { [CLIENT_SESSION_HEADER]: hostCallClient.getStore()!.session! } : {}),
      ...extraHeaders },
  });
  if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
  const stopped = new AbortController();
  let heartbeatFailure: unknown;
  let heartbeatInFlight: Promise<void> | undefined;
  const keepAlive = setInterval(() => {
    if (heartbeatInFlight) return;
    heartbeatInFlight = (async () => {
      const renewedCredentials = await options.credentials();
      if (!renewedCredentials.sessionToken || options.currentRun() !== options.runId) {
        throw new Error('Current Crew host identity unavailable');
      }
      if (!(await deliveredTokenIsFresh(options))) {
        const renewedSigner = new SignatureV4({ credentials: renewedCredentials, region: options.region,
          service: 'execute-api', sha256: Hash.bind(null, 'sha256') });
        const refreshAgain = await renewedSigner.sign({ method: 'POST', protocol: 'https:', hostname: url.hostname,
          path: refreshPath, body: refreshBody,
          headers: { host: url.hostname, 'content-type': 'application/json', 'x-agent-runid': options.runId } });
        if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
        const renewed = await (options.send ?? fetch)(`${url.origin}${refreshPath}`, {
          method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15_000), headers: refreshAgain.headers, body: refreshBody,
        });
        await renewed.body?.cancel();
        if (renewed.status !== 204) throw new CrewHostModelRouteError(renewed.status);
      }
      await activity(true);
    })().catch(error => { heartbeatFailure = error; stopped.abort(); }).finally(() => { heartbeatInFlight = undefined; });
  }, 30_000);
  let finished = false;
  return {
    url, path, headers: signed.headers as Record<string, string>, payload, stopped,
    failure: () => heartbeatFailure,
    finish: async (completed: boolean) => {
      if (finished) return;
      finished = true;
      clearInterval(keepAlive);
      await heartbeatInFlight;
      if (heartbeatFailure) throw heartbeatFailure;
      if (completed && options.currentRun() === options.runId) {
        // The idle mark is auto-stop bookkeeping, not part of the reply: it held every finished call
        // for another API Gateway round trip (~0.5 s). A mark that fails or lands late is harmless —
        // every mark stamps assistant activity, and the next call's busy mark or heartbeat supersedes it.
        void activity(false).catch(() => undefined);
      }
    },
  };
}

export async function invokeCrewHostModel(options: CrewHostRouteOptions, body: unknown, signal?: AbortSignal, model?: string): Promise<unknown> {
  signal?.throwIfAborted();
  const call = await beginHostModelCall(options, body, 'invoke', {}, model);
  let completed = false;
  try {
    signal?.throwIfAborted();
    const response = await (options.send ?? fetch)(`${call.url.origin}${call.path}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.any([AbortSignal.timeout(3_660_000), call.stopped.signal, ...(signal ? [signal] : [])]),
      headers: call.headers, body: call.payload,
    });
    if (call.failure()) {
      await response.body?.cancel();
      throw call.failure();
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new CrewHostModelRouteError(response.status);
    }
    if (options.currentRun() !== options.runId) {
      await response.body?.cancel();
      throw new Error('Crew host run was fenced');
    }
    const reply = await response.json();
    signal?.throwIfAborted();
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
    completed = true;
    return reply;
  } catch (error) {
    throw call.failure() ?? error;
  } finally {
    await call.finish(completed);
  }
}

/** Split a byte stream into Server-Sent Events; comment lines (keepalives) are dropped. */
export async function* parseServerSentEvents(body: AsyncIterable<Uint8Array>): AsyncGenerator<HostModelStreamEvent> {
  const decoder = new TextDecoder();
  let buffered = '';
  let event = '';
  const data: string[] = [];
  const flush = (): HostModelStreamEvent | undefined => {
    if (!event && !data.length) return undefined;
    const parsed = { event: event || 'message', data: data.join('\n') };
    event = '';
    data.length = 0;
    return parsed;
  };
  for await (const chunk of body) {
    buffered += decoder.decode(chunk, { stream: true });
    let newline: number;
    while ((newline = buffered.indexOf('\n')) >= 0) {
      const line = buffered.slice(0, newline).replace(/\r$/, '');
      buffered = buffered.slice(newline + 1);
      if (line === '') {
        const parsed = flush();
        if (parsed) yield parsed;
      } else if (line.startsWith(':')) {
        continue;
      } else if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        data.push(line.slice(5).replace(/^ /, ''));
      }
    }
  }
  buffered += decoder.decode();
  if (buffered.trim()) {
    const line = buffered.replace(/\r$/, '');
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
  }
  const parsed = flush();
  if (parsed) yield parsed;
}

/**
 * The streaming twin of {@link invokeCrewHostModel}: the same proof, refresh, busy bracket and
 * heartbeat, against `/agent/model/{model}/invoke-with-response-stream` with `accept:
 * text/event-stream`, yielding the gateway's Anthropic-format events as they arrive so Claude Code
 * sees tokens while Bedrock is still generating (a 23 s one-word turn on dev 2026-10-21 waited for
 * the whole reply). A refusal before the first byte is a thrown route error, exactly like the
 * non-streaming call; `signal` (the caller going away) cancels the upstream body.
 */
export async function* invokeCrewHostModelStream(options: CrewHostRouteOptions, body: unknown,
  signal?: AbortSignal, model?: string): AsyncGenerator<HostModelStreamEvent> {
  signal?.throwIfAborted();
  const call = await beginHostModelCall(options, body, 'invoke-with-response-stream', { accept: 'text/event-stream' }, model);
  let completed = false;
  try {
    signal?.throwIfAborted();
    const response = await (options.send ?? fetch)(`${call.url.origin}${call.path}`, {
      method: 'POST', redirect: 'error', headers: call.headers, body: call.payload,
      signal: AbortSignal.any([AbortSignal.timeout(3_660_000), call.stopped.signal, ...(signal ? [signal] : [])]),
    });
    if (call.failure()) {
      await response.body?.cancel();
      throw call.failure();
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new CrewHostModelRouteError(response.status);
    }
    if (options.currentRun() !== options.runId || !response.body) {
      await response.body?.cancel();
      throw new Error('Crew host run was fenced');
    }
    for await (const event of parseServerSentEvents(response.body as unknown as AsyncIterable<Uint8Array>)) {
      if (call.failure()) throw call.failure();
      if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
      yield event;
    }
    signal?.throwIfAborted();
    if (options.currentRun() !== options.runId) throw new Error('Crew host run was fenced');
    completed = true;
  } catch (error) {
    throw call.failure() ?? error;
  } finally {
    call.stopped.abort();
    await call.finish(completed);
  }
}
