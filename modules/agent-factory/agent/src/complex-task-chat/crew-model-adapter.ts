import { randomBytes } from 'node:crypto';
import { createServer, IncomingMessage, ServerResponse } from 'node:http';
import { AddressInfo } from 'node:net';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { z } from 'zod';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { allowedCrewOrchestrationPath, clientUserAgentHeader, hostCallClient } from './crew-host-model-route';
import { ChatModelRequest, DEFAULT_MODEL_REQUEST_LIMITS, ModelRequestLimits, SystemBlock, buildModelRequestSchema, cacheHint, modelOutputBlock, modelRequestSchema, modelText } from './gateway/chat-model-contract';

function stripCacheHint(block: unknown): unknown {
  if (typeof block !== 'object' || block === null || !('cache_control' in block)) return block;
  const { cache_control, ...content } = block;
  return cacheHint.safeParse(cache_control).success ? content : block;
}

const systemText = z.object({ type: z.literal('text'), text: z.string(), cache_control: cacheHint.optional() }).strict();
const systemSchema = z.union([z.string(), z.array(systemText)]);
function buildSdkRequestSchema(limits: ModelRequestLimits) {
  // The sandbox strips validated cache hints before the strict contract check; a Dev Box host keeps
  // them so Bedrock serves Claude Code's ~115k-token prefix from cache (contract `cacheMarkers`).
  const normalize = limits.cacheMarkers ? (block: unknown) => block : stripCacheHint;
  const marked = <T extends z.ZodObject>(schema: T) => limits.cacheMarkers ? schema.extend({ cache_control: cacheHint.optional() }).strict() : schema;
  const sdkToolResult = marked(z.object({ type: z.literal('tool_result'), tool_use_id: z.string().min(1).max(200),
    content: z.union([z.string(), z.array(systemText).min(1).max(limits.blocks)]), is_error: z.boolean().optional() }).strict());
  // `modelText` carries the chat sandbox's fixed 64 KiB ceiling. A Dev Box host forwards whatever
  // the client composed: Kiro Crew's first turn on dev (2026-11-03) put its context in one user text
  // block above 64 KiB and every new session answered 400 messages.0.content.1.text:too_big, so the
  // text block follows the profile's `contentChars` (1 MB on a host) like the outgoing schema does.
  const sdkText = modelText.extend({ text: z.string().max(limits.contentChars) }).strict();
  const sdkOutputBlock = z.union([sdkText, ...modelOutputBlock.options.filter(option => option !== modelText)]);
  const sdkBlock = limits.cacheMarkers
    ? z.union([...sdkOutputBlock.options.map(option => marked(option)), sdkToolResult])
    : z.union([sdkOutputBlock, sdkToolResult]);
  const sdkMessage = z.union([
    z.object({ role: z.enum(['user', 'assistant']), content: z.union([z.string().min(1).max(limits.contentChars),
      z.array(z.preprocess(normalize, sdkBlock)).min(1).max(limits.blocks)]),
      // Claude Code 2.1.296 puts `output_config: { effort }` on the last user message whenever a
      // non-default model is selected (dev 2026-10-25: every `--model opus/haiku` turn answered 400
      // messages.1:unrecognized_keys). Accepted here, dropped before forwarding like the top-level key.
      output_config: z.record(z.string(), z.unknown()).optional() }).strict(),
    // With a non-default model Claude Code attaches `output_config: { effort }` to its trailing *system*
    // reminder as well (gen 48, 2026-10-25: `--model opus` still answered messages.1:unrecognized_keys
    // after the user/assistant fix). Accepted and dropped like everywhere else.
    z.object({ role: z.literal('system'), content: systemSchema, output_config: z.record(z.string(), z.unknown()).optional() }).strict()]);
  const toolSchema = buildModelRequestSchema(limits).shape.tools.unwrap().element;
  return z.object({
    model: z.string().min(1).max(256),
    messages: z.array(sdkMessage).min(1).max(limits.messages),
    system: systemSchema.optional(),
    // Clamped to `limits.maxTokens` when forwarded (below); Claude Code asks for 128000 on Opus and the
    // old 64000 ceiling answered 400 max_tokens:too_big for every non-default model (dev 2026-10-25).
    max_tokens: z.number().int().min(1),
    tools: z.array(z.preprocess(normalize, toolSchema)).max(limits.tools).optional(),
    stream: z.boolean().optional(),
    temperature: z.number().min(0).max(1).optional(),
    metadata: z.object({ user_id: z.string().max(256).optional() }).strict().optional(),
    // Claude Code 2.1.296 sends `{ type: 'adaptive', display: 'updates' }` on every turn (#8223 cause 44);
    // the adapter never forwards thinking configuration, so any object is accepted and dropped.
    thinking: z.record(z.string(), z.unknown()).optional(),
    context_management: z.object({ edits: z.array(z.object({ type: z.literal('clear_thinking_20251015'),
      keep: z.literal('all') }).strict()).max(1) }).strict().optional(),
    // Discarded, never forwarded: Claude Code 2.1.296 sends structured-output `format` next to `effort`
    // (#8223 cause 42), so any object is accepted here.
    output_config: z.record(z.string(), z.unknown()).optional(),
    // Unknown top-level keys are stripped, not refused: only the fields named above are forwarded, so a
    // new Claude Code request key must not break every turn again (#8223 cause 42). The forwarded parts
    // (messages, content blocks, tools, system) and the identity-bearing `metadata` stay strict.
  });
}

function buildResponsesRequest(body: unknown, modelId: string, limits: ModelRequestLimits) {
  const textPart = z.object({ type: z.enum(['input_text', 'output_text']), text: z.string().max(limits.contentChars) }).passthrough();
  const inputItem = z.discriminatedUnion('type', [
    z.object({ type: z.literal('message'), role: z.enum(['system', 'developer', 'user', 'assistant']),
      content: z.union([z.string().max(limits.contentChars), z.array(textPart).max(limits.blocks)]) }).passthrough(),
    z.object({ type: z.literal('function_call'), call_id: z.string().min(1).max(200),
      name: z.string().min(1).max(128), arguments: z.string().max(limits.contentChars) }).passthrough(),
    z.object({ type: z.literal('custom_tool_call'), call_id: z.string().min(1).max(200),
      name: z.string().min(1).max(128), input: z.string().max(limits.contentChars) }).passthrough(),
    z.object({ type: z.literal('function_call_output'), call_id: z.string().min(1).max(200),
      output: z.string().max(limits.contentChars) }).passthrough(),
    z.object({ type: z.literal('custom_tool_call_output'), call_id: z.string().min(1).max(200),
      output: z.string().max(limits.contentChars) }).passthrough(),
    z.object({ type: z.literal('reasoning') }).passthrough(),
  ]);
  const parsed = z.object({
    model: z.literal(modelId), instructions: z.string().max(limits.systemChars).optional(),
    input: z.union([z.string().min(1).max(limits.contentChars), z.array(inputItem).min(1).max(limits.messages)]),
    // Only `function` and `custom` tools become Claude tools. Every other family Codex declares
    // (`tool_search`, `web_search`, `local_shell`, image or MCP tools — the set grows with each CLI
    // release; 0.164.0 refused the whole turn on dev 2026-10-23 at tools[7]) is accepted and dropped.
    tools: z.array(z.preprocess(
      tool => {
        const type = typeof tool === 'object' && tool !== null ? (tool as { type?: unknown }).type : undefined;
        return typeof type !== 'string' || type === 'function' || type === 'custom' ? tool : { type: 'other' };
      },
      z.discriminatedUnion('type', [
        z.object({ type: z.literal('function'), name: z.string().min(1).max(128),
          description: z.string().max(limits.toolDescriptionChars).default(''),
          parameters: z.record(z.string(), z.json()) }).passthrough(),
        z.object({ type: z.literal('custom'), name: z.string().min(1).max(128),
          description: z.string().max(limits.toolDescriptionChars).default('') }).passthrough(),
        z.object({ type: z.literal('other') }).strict(),
      ]))).max(limits.tools).optional(),
    stream: z.boolean().optional(),
  }).passthrough().parse(body);
  const identityKeys = ['user_id', 'root_human_id', 'agent_run_id', 'run_id', 'org_id'];
  for (const candidate of [parsed, parsed.metadata, parsed.client_metadata]) {
    if (typeof candidate === 'object' && candidate !== null &&
        identityKeys.some(key => Object.prototype.hasOwnProperty.call(candidate, key))) {
      throw new Error('Responses run identity cannot be supplied by the client');
    }
  }
  const messages: ChatModelRequest['messages'] = [];
  const system: string[] = parsed.instructions ? [parsed.instructions] : [];
  if (typeof parsed.input === 'string') messages.push({ role: 'user', content: parsed.input });
  else for (const item of parsed.input) {
    if (item.type === 'reasoning') continue;
    if (item.type === 'message') {
      const text = typeof item.content === 'string' ? item.content : item.content.map(part => part.text).join('\n');
      if (item.role === 'system' || item.role === 'developer') system.push(text);
      else if (text) messages.push({ role: item.role, content: text });
    } else if (item.type === 'function_call') {
      const input = z.record(z.string(), z.json()).parse(JSON.parse(item.arguments));
      messages.push({ role: 'assistant', content: [{ type: 'tool_use', id: item.call_id, name: item.name, input }] });
    } else if (item.type === 'custom_tool_call') {
      messages.push({ role: 'assistant', content: [{ type: 'tool_use', id: item.call_id, name: item.name, input: { input: item.input } }] });
    } else if (item.type === 'function_call_output' || item.type === 'custom_tool_call_output') {
      messages.push({ role: 'user', content: [{ type: 'tool_result', tool_use_id: item.call_id, content: item.output }] });
    }
  }
  const modelRequest: ChatModelRequest = { messages, ...(system.length ? { system: system.join('\n') } : {}),
    max_tokens: limits.maxTokens,
    ...(parsed.tools ? { tools: parsed.tools.flatMap(tool => tool.type === 'function'
      ? [{ name: tool.name, description: tool.description, input_schema: tool.parameters }]
      : tool.type === 'custom' ? [{ name: tool.name, description: tool.description,
        input_schema: { type: 'object', properties: { input: { type: 'string' } }, required: ['input'] } }] : []) } : {}) };
  return { modelRequest, customTools: new Set(parsed.tools?.filter(tool => tool.type === 'custom').map(tool => tool.name) ?? []) };
}

type ResponsesOutputItem = { id: string; type: 'message'; role: 'assistant'; status: 'completed';
  content: { type: 'output_text'; text: string; annotations: never[] }[] }
  | { id: string; type: 'function_call'; status: 'completed'; call_id: string; name: string; arguments: string }
  | { id: string; type: 'custom_tool_call'; status: 'completed'; call_id: string; name: string; input: string };

function writeResponses(response: ServerResponse, modelId: string, result: Awaited<ReturnType<ChatDataClient['invokeModel']>>,
  stream: boolean, customTools: Set<string>): void {
  const id = `resp_${randomBytes(12).toString('hex')}`;
  const output = result.content.flatMap<ResponsesOutputItem>((block, index) => block.type === 'text'
    ? [{ id: `msg_${index}`, type: 'message', role: 'assistant', status: 'completed',
      content: [{ type: 'output_text', text: block.text, annotations: [] }] }]
    : block.type === 'tool_use'
      ? [customTools.has(block.name)
        ? { id: `call_${index}`, type: 'custom_tool_call', status: 'completed', call_id: block.id,
          name: block.name, input: typeof block.input.input === 'string' ? block.input.input : '' }
        : { id: `call_${index}`, type: 'function_call', status: 'completed', call_id: block.id,
          name: block.name, arguments: JSON.stringify(block.input) }]
      : []);
  const usage = { input_tokens: result.usage.input_tokens, output_tokens: result.usage.output_tokens,
    total_tokens: result.usage.input_tokens + result.usage.output_tokens };
  const completed = { id, object: 'response', created_at: Math.floor(Date.now() / 1000), model: modelId,
    status: 'completed', output, usage, error: null, incomplete_details: null };
  if (!stream) {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(completed));
    return;
  }
  response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
  const event = (type: string, fields: object) => response.write(`data: ${JSON.stringify({ type, ...fields })}\n\n`);
  event('response.created', { response: { ...completed, status: 'in_progress', output: [], usage: null } });
  for (const [output_index, item] of output.entries()) {
    event('response.output_item.added', { response_id: id, output_index, item: { ...item, status: 'in_progress',
      ...(item.type === 'message' ? { content: [] } : { arguments: '' }) } });
    if (item.type === 'message') {
      const part = item.content[0];
      event('response.content_part.added', { response_id: id, output_index, content_index: 0,
        part: { ...part, text: '' } });
      event('response.output_text.delta', { response_id: id, output_index, content_index: 0, delta: part.text });
      event('response.output_text.done', { response_id: id, output_index, content_index: 0, text: part.text });
      event('response.content_part.done', { response_id: id, output_index, content_index: 0, part });
    } else if (item.type === 'function_call') {
      event('response.function_call_arguments.delta', { response_id: id, output_index, delta: item.arguments });
      event('response.function_call_arguments.done', { response_id: id, output_index, arguments: item.arguments });
    } else {
      event('response.custom_tool_call_input.delta', { response_id: id, output_index, delta: item.input });
      event('response.custom_tool_call_input.done', { response_id: id, output_index, input: item.input });
    }
    event('response.output_item.done', { response_id: id, output_index, item });
  }
  event('response.completed', { response: completed });
  response.end('data: [DONE]\n\n');
}

function respond(response: ServerResponse, status: number, message: string, type = 'invalid_request_error',
  headers: Record<string, string> = {}): void {
  response.writeHead(status, { 'content-type': 'application/json', ...headers });
  response.end(JSON.stringify({ type: 'error', error: { type, message } }));
}

type SdkFailure = { status: number; type: string; message: string; headers: Record<string, string> };

/**
 * The SDK-facing status for a failed gateway call (#8223 cause 48).
 *
 * Claude Code treats 401/403 from its API as "not signed in": on dev 2026-10-21 a transient local
 * failure answered as `403 Gateway unavailable` made the Claude agent adapter latch the backend as
 * signed out, and the owner saw "Your session has expired" on a fresh chat. So the adapter never
 * answers 401/403 for anything but a wrong loopback secret: an unavailable gateway or an unexpected
 * local error is a 503 the SDK retries with backoff; a rate limit is a 429; a refusal the gateway
 * made about this run (denied, fenced, invalid) is a 400 that ends the turn with a readable reason.
 */
function sdkFailure(error: unknown, phase: 'request' | 'invoke'): SdkFailure {
  if (error instanceof ChatDataError) {
    if (error.code === 'unavailable') return { status: 503, type: 'api_error', message: 'Gateway unavailable', headers: { 'retry-after': '2' } };
    if (error.code === 'rate_limited') return { status: 429, type: 'rate_limited_error', message: 'Gateway rate_limited',
      headers: { 'retry-after': String(Math.max(1, Math.ceil((error.retryAfterMs ?? 2000) / 1000))) } };
    if (error.code === 'scope_mismatch') return { status: 400, type: 'invalid_request_error', message: 'Gateway run fenced', headers: {} };
    return { status: 400, type: 'invalid_request_error', message: `Gateway ${error.code}`, headers: {} };
  }
  if (phase === 'invoke') return { status: 503, type: 'api_error', message: 'Gateway unavailable', headers: { 'retry-after': '2' } };
  return { status: 400, type: 'invalid_request_error', message: error instanceof Error ? error.message : 'Messages request failed', headers: {} };
}

/** One structured stderr line per failed gateway call (the adp-crew journal); never a body or a secret. */
function logFailure(route: 'messages' | 'tools' | 'responses' | 'github', error: unknown, failure: SdkFailure, startedAt: number, report = defaultReport): void {
  report(JSON.stringify({
    event: 'crew_model_adapter_failure', route, status: failure.status,
    error_class: error instanceof Error ? error.name : typeof error,
    gateway_code: error instanceof ChatDataError ? error.code : undefined,
    gateway_status: error instanceof ChatDataError ? error.status : undefined,
    message: error instanceof ChatDataError ? undefined : error instanceof Error ? error.message.slice(0, 200) : undefined,
    // The route's own reason behind a local `unavailable` (#8223 cause 55: the journal said only
    // "unavailable" while every call failed on an expired token read). Static route text, never a body.
    cause: error instanceof ChatDataError && error.cause instanceof Error ? error.cause.message.slice(0, 200) : undefined,
    elapsed_ms: Date.now() - startedAt,
  }));
}

const defaultReport = (line: string) => { process.stderr.write(`${line}\n`); };

/** A served GitHub token is reused until this close to the gateway's horizon, then minted again (#8555). */
export const GITHUB_TOKEN_REFRESH_MARGIN_SECONDS = 300;
export const GITHUB_TOKEN_HORIZON_SECONDS = 600;

/**
 * The tools catalogue (`GET /tools`) is served from this process's memory (#8835).
 *
 * Kiro Crew probes the adp-tools MCP server with a 15 s limit at start and quarantines it after three
 * timeouts; Claude Code lists the tools again on every Crew turn. Through the signed host route the
 * listing cost 15.5 s cold and 0.4–3.8 s warm on dev 2026-10-26 (gen 51), and the gateway's own 60 s
 * cache lives per replica (12 of them), so a probe rarely hit it. The catalogue changes only with the
 * owner's policy, so the adapter answers from the last successful listing for up to
 * TOOLS_LISTING_MAX_AGE_MS, refreshing in the background once it is older than TOOLS_LISTING_FRESH_MS.
 * Concurrent listings share one upstream call. Tool calls themselves are never cached.
 */
export const TOOLS_LISTING_FRESH_MS = 60_000;
export const TOOLS_LISTING_MAX_AGE_MS = 600_000;

type GithubTokenMint = { requestGithubToken?: () => Promise<{ token: string; login: string; expires_at: number }> };
type OpenAIResponsesRelay = { requestOpenAIResponses?: (body: unknown, abort?: AbortSignal) => Promise<{
  status: number; contentType: string; body: ReadableStream<Uint8Array> | null; finish: (completed: boolean) => Promise<void>; stopped: AbortController }> };
/** Models the gateway serves on its OpenAI passthrough rather than the Bedrock invoke route. */
const OPENAI_MODEL = /^(?:[a-z]{2,6}\.)?openai\.[A-Za-z0-9_.:-]{1,200}$/;

/**
 * The step that fixes a refused GitHub token, in the words the box's `gh`/`git` print (#8555).
 * The gateway's status is the discriminator; the texts are constants and never carry a body.
 */
function githubFailure(error: unknown): SdkFailure {
  if (error instanceof ChatDataError && error.status !== undefined) {
    const named: Record<number, string> = {
      404: 'GitHub not connected: connect your GitHub account with repository access in ADP Settings → Connections',
      403: 'GitHub consent required: grant repository access and allow agents to use the account in ADP Settings → Connections',
      409: 'GitHub reconnect required: reconnect GitHub in ADP Settings → Connections (or this box\'s lease ended)',
      503: 'GitHub access for Dev Boxes is not enabled on this deployment',
    };
    const message = named[error.status];
    if (message) return { status: error.status, type: 'api_error', message, headers: {} };
  }
  return sdkFailure(error, 'invoke');
}

async function readBody(request: IncomingMessage, maxBytes: number): Promise<unknown> {
  // Bounded single buffer: the body is parsed once, never copied; the bound is the caller's limit
  // profile (64 KiB for the chat sandbox, 8 MiB for a Dev Box host driving Claude Code — #8223 cause 36).
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new Error(`Messages request exceeds ${maxBytes} bytes`);
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

/**
 * Relay the gateway's streamed Anthropic events to the SDK as they arrive (Dev Box host path).
 *
 * The first event is awaited before headers are written so a gateway refusal still answers with the
 * SDK error shape; after that, every event is forwarded verbatim except `message_start`, whose
 * `model` is rewritten to the approved id the SDK asked for, and `ping`, which the SDK does not
 * need. The SDK going away cancels the upstream body; a run or turn change mid-stream destroys the
 * response rather than letting a fenced run finish speaking.
 */
async function relayStream(client: ChatDataClient, modelId: string, modelRequest: ChatModelRequest, request: IncomingMessage,
  response: ServerResponse, signal: AbortSignal | undefined, fenced: () => boolean, forwardModel = false): Promise<void> {
  const upstream = new AbortController();
  const bounded = signal ? AbortSignal.any([signal, upstream.signal]) : upstream.signal;
  const iterator = (forwardModel
    ? (client.invokeModelStream! as (...args: unknown[]) => ReturnType<NonNullable<ChatDataClient['invokeModelStream']>>)(
      `sdk_${randomBytes(12).toString('hex')}`, modelRequest, bounded, modelId)
    : client.invokeModelStream!(`sdk_${randomBytes(12).toString('hex')}`, modelRequest, bounded))[Symbol.asyncIterator]();
  const first = await iterator.next();
  signal?.throwIfAborted();
  if (fenced()) {
    await iterator.return?.();
    throw new ChatDataError('scope_mismatch');
  }
  let finished = false;
  // `response` closes when the SDK's socket goes away mid-reply (an aborted fetch), which is the
  // signal to stop paying for upstream tokens; it also closes after a normal end, hence `finished`.
  const onClose = () => { if (!finished) { upstream.abort(); void iterator.return?.(); } };
  response.on('close', onClose);
  response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
  try {
    let next = first;
    while (!next.done) {
      const { event, data } = next.value;
      if (fenced()) throw new ChatDataError('scope_mismatch');
      if (event !== 'ping') {
        let payload = data;
        if (event === 'message_start') {
          try {
            const parsed = JSON.parse(data) as { message?: { model?: string } };
            if (parsed.message && typeof parsed.message === 'object') parsed.message.model = modelId;
            payload = JSON.stringify(parsed);
          } catch { /* forwarded as received */ }
        }
        response.write(`event: ${event}\ndata: ${payload}\n\n`);
      }
      next = await iterator.next();
    }
    finished = true;
    response.end();
  } finally {
    response.off('close', onClose);
    if (!finished) upstream.abort();
  }
}

export async function startCrewModelAdapter(client: ChatDataClient, modelId: string, signal?: AbortSignal, deliverResponse = true,
  limits: ModelRequestLimits = DEFAULT_MODEL_REQUEST_LIMITS, report: (line: string) => void = defaultReport) {
  const sdkRequestSchema = buildSdkRequestSchema(limits);
  const outgoingSchema = limits === DEFAULT_MODEL_REQUEST_LIMITS ? modelRequestSchema : buildModelRequestSchema(limits);
  const secret = randomBytes(32).toString('hex');
  const wireModelId = modelId;
  let gatewayError: ChatDataError | undefined;
  let githubToken: { token: string; login: string; expires_at: number } | undefined;
  let scope = { client, modelId, signal, deliverResponse };
  let scopeCancellation = new AbortController();
  const bindTurn = (client: ChatDataClient, modelId: string, signal: AbortSignal, deliverResponse = true) => {
    scopeCancellation.abort();
    githubToken = undefined;
    scopeCancellation = new AbortController();
    const bounded = AbortSignal.any([signal, scopeCancellation.signal]);
    scope = { client: client.withSignal(bounded), modelId, signal: bounded, deliverResponse };
    gatewayError = undefined;
    listing = undefined;
    listingInFlight = undefined;
  };
  // #8835: one remembered catalogue per bound scope (a new turn or run never shares it).
  let listing: { owner: object; body: unknown; at: number } | undefined;
  let listingInFlight: { owner: object; promise: Promise<{ status: number; body: unknown }> } | undefined;
  const fetchListing = (current: typeof scope): Promise<{ status: number; body: unknown }> => {
    if (listingInFlight?.owner === current) return listingInFlight.promise;
    const promise = current.client.requestTools('GET', '/tools', undefined).then(result => {
      if (result.status === 200 && current === scope) listing = { owner: current, body: result.body, at: Date.now() };
      return result;
    }).finally(() => { if (listingInFlight?.promise === promise) listingInFlight = undefined; });
    listingInFlight = { owner: current, promise };
    return promise;
  };
  const handleRequest = async (request: IncomingMessage, response: ServerResponse) => {
    if (request.headers['x-api-key'] !== secret && request.headers.authorization !== `Bearer ${secret}`) {
      respond(response, 401, 'Unauthorized');
      return;
    }
    if (request.method === 'HEAD' && request.url === '/api/hello') {
      response.writeHead(204);
      response.end();
      return;
    }
    if (request.method === 'POST' && request.url === '/aws/credentials') {
      const current = scope;
      try {
        current.signal?.throwIfAborted();
        const input = await readBody(request, 4096);
        const mint = (current.client as unknown as { requestAwsCredentials?: (input: unknown) => Promise<unknown> }).requestAwsCredentials;
        if (!mint) { respond(response, 503, 'AWS access is unavailable on this host', 'api_error'); return; }
        const result = await mint.call(current.client, input);
        current.signal?.throwIfAborted();
        if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
        response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        response.end(JSON.stringify(result));
      } catch (error) {
        // Never log the provider body, even on schema/transport failures.
        const status = error instanceof ChatDataError ? error.status ?? 503 : 503;
        respond(response, status, 'AWS access refused; check the selected ADP connection and its agent access setting', 'api_error');
      }
      return;
    }
    if (request.method === 'GET' && request.url === '/github/token') {
      // #8555: the box's `gh` wrapper and git credential helper ask here; the owner's token is
      // minted through the signed host route, kept in memory only and reused inside its horizon.
      const current = scope;
      const startedAt = Date.now();
      try {
        current.signal?.throwIfAborted();
        const mint = (current.client as unknown as GithubTokenMint).requestGithubToken;
        if (!mint) {
          respond(response, 503, 'GitHub access is unavailable on this host', 'api_error');
          return;
        }
        if (!githubToken || githubToken.expires_at - Math.floor(Date.now() / 1000) < GITHUB_TOKEN_REFRESH_MARGIN_SECONDS) {
          const minted = await mint.call(current.client);
          current.signal?.throwIfAborted();
          if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
          const issuedAt = Math.floor(Date.now() / 1000);
          if (!Number.isSafeInteger(minted.expires_at) || minted.expires_at <= issuedAt) throw new ChatDataError('unavailable', 409);
          githubToken = { ...minted, expires_at: Math.min(minted.expires_at, issuedAt + GITHUB_TOKEN_HORIZON_SECONDS) };
        }
        response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        response.end(JSON.stringify(githubToken));
      } catch (error) {
        githubToken = undefined;
        const failure = githubFailure(error);
        logFailure('github', error, failure, startedAt, report);
        respond(response, failure.status, failure.message, failure.type, failure.headers);
      }
      return;
    }
    if (request.url && (/^\/(?:agent\/)?orchestration(?:[/?]|$)/.test(request.url) ||
        /^\/products(?:[/?]|$)/.test(request.url) || /^\/github\/status(?:[/?]|$)/.test(request.url))) {
      const path = request.url.startsWith('/agent/orchestration/') ? request.url.slice('/agent'.length) :
        request.url.startsWith('/products') || request.url.startsWith('/github/status') ? `/orchestration${request.url}` : '';
      if ((request.method === 'GET' || request.method === 'POST') && allowedCrewOrchestrationPath(request.method, path)) {
        const current = scope;
        const relay = (current.client as unknown as { requestOrchestration?: (method: 'GET' | 'POST', path: string, body?: unknown) => Promise<{
          status: number; body: unknown }> }).requestOrchestration;
        if (!relay) { respond(response, 404, 'Orchestration route unavailable'); return; }
        try {
          current.signal?.throwIfAborted();
          const body = request.method === 'POST' ? await readBody(request, limits.bodyBytes) : undefined;
          const result = await relay.call(current.client, request.method, path, body);
          if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
          response.writeHead(result.status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
          response.end(JSON.stringify(result.body));
        } catch (error) {
          const failure = sdkFailure(error, 'invoke');
          respond(response, failure.status, failure.message, failure.type, failure.headers);
        }
        return;
      }
      respond(response, 404, 'Orchestration route unavailable');
      return;
    }
    if ((request.method === 'GET' || request.method === 'POST') && /^\/tools(?:[/?]|$)/.test(request.url ?? '')) {
      const current = scope;
      const startedAt = Date.now();
      let phase: 'request' | 'invoke' = 'request';
      try {
        current.signal?.throwIfAborted();
        const body = request.method === 'POST' ? await readBody(request, limits.bodyBytes) : undefined;
        if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
        phase = 'invoke';
        const catalogue = request.method === 'GET' && request.url === '/tools';
        const remembered = catalogue && listing?.owner === current ? listing : undefined;
        const age = remembered ? Date.now() - remembered.at : Number.POSITIVE_INFINITY;
        let result: { status: number; body: unknown };
        if (remembered && age < TOOLS_LISTING_MAX_AGE_MS) {
          result = { status: 200, body: remembered.body };
          if (age >= TOOLS_LISTING_FRESH_MS && listingInFlight?.owner !== current) {
            const refreshStartedAt = Date.now();
            fetchListing(current).catch((error: unknown) => logFailure('tools', error, sdkFailure(error, 'invoke'), refreshStartedAt, report));
          }
        } else {
          result = catalogue ? await fetchListing(current) : await current.client.requestTools(request.method, request.url!, body);
        }
        current.signal?.throwIfAborted();
        if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
        response.writeHead(result.status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        response.end(JSON.stringify(result.body));
      } catch (error) {
        const failure = sdkFailure(error, phase);
        logFailure('tools', error, failure, startedAt, report);
        respond(response, failure.status, failure.message === 'Gateway unavailable' ? 'Crew tools unavailable' : failure.message,
          failure.type, failure.headers);
      }
      return;
    }
    if (limits.cacheMarkers && request.method === 'POST' && request.url === '/v1/responses' &&
      request.headers['content-type']?.startsWith('application/json')) {
      const current = scope;
      const startedAt = Date.now();
      let phase: 'request' | 'invoke' = 'request';
      try {
        current.signal?.throwIfAborted();
        const body = await readBody(request, limits.bodyBytes);
        const openaiModel = typeof (body as { model?: unknown })?.model === 'string' && OPENAI_MODEL.test((body as { model: string }).model);
        const relay = (current.client as unknown as OpenAIResponsesRelay).requestOpenAIResponses;
        if (openaiModel && relay) {
          // GPT models (dev 2026-10-26): no translation — Codex's own Responses request and the gateway's
          // reply (JSON or SSE) pass through byte for byte under the signed host identity.
          current.signal?.throwIfAborted();
          if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
          phase = 'invoke';
          const downstream = new AbortController();
          const bounded = AbortSignal.any([downstream.signal, scopeCancellation.signal, ...(current.signal ? [current.signal] : [])]);
          const onClose = () => { if (!response.writableFinished) downstream.abort(); };
          response.on('close', onClose);
          let upstream: Awaited<ReturnType<NonNullable<OpenAIResponsesRelay['requestOpenAIResponses']>>> | undefined;
          let completed = false;
          try {
            if (response.destroyed) downstream.abort();
            bounded.throwIfAborted();
            upstream = await relay.call(current.client, body, bounded);
            bounded.throwIfAborted();
            response.writeHead(upstream.status, { 'content-type': upstream.contentType, 'cache-control': 'no-store' });
            await pipeline(upstream.body ? Readable.fromWeb(upstream.body) : Readable.from([]), response,
              { signal: AbortSignal.any([bounded, upstream.stopped.signal]) });
            completed = true;
          } finally {
            response.off('close', onClose);
            if (upstream) {
              upstream.stopped.abort();
              if (!completed && upstream.body && !upstream.body.locked) void upstream.body.cancel().catch(() => undefined);
              await upstream.finish(completed);
            }
          }
          return;
        }
        const requested = limits.cacheMarkers && typeof (body as { model?: unknown })?.model === 'string' &&
          /^[A-Za-z0-9_.:-]{1,256}$/.test((body as { model: string }).model) ? (body as { model: string }).model : current.modelId;
        const { modelRequest, customTools } = buildResponsesRequest(body, requested, limits);
        if (!outgoingSchema.safeParse(modelRequest).success) throw new Error('Unsupported Responses content');
        current.signal?.throwIfAborted();
        if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
        phase = 'invoke';
        const result = requested !== current.modelId
          ? await (current.client.invokeModel as (...args: unknown[]) => ReturnType<ChatDataClient['invokeModel']>)(
            `sdk_${randomBytes(12).toString('hex')}`, modelRequest, undefined, current.deliverResponse, undefined, requested)
          : await current.client.invokeModel(`sdk_${randomBytes(12).toString('hex')}`, modelRequest, undefined, current.deliverResponse);
        if (result.modelId !== requested) throw new ChatDataError('scope_mismatch');
        current.signal?.throwIfAborted();
        if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
        writeResponses(response, requested, result, (body as { stream?: boolean }).stream === true, customTools);
      } catch (error) {
        if (current === scope && error instanceof ChatDataError) gatewayError = error;
        const failure = sdkFailure(error, phase);
        logFailure('responses', error, failure, startedAt, report);
        if (response.headersSent) response.destroy(error as Error);
        else respond(response, failure.status, failure.message, failure.type, failure.headers);
      }
      return;
    }
    if (request.method !== 'POST' || !['/v1/messages', '/v1/messages?beta=true'].includes(request.url ?? '') ||
      !request.headers['content-type']?.startsWith('application/json')) {
      respond(response, 400, 'Only JSON POST /v1/messages is supported');
      return;
    }
    const current = scope;
    const { client, modelId, signal, deliverResponse } = current;
    const startedAt = Date.now();
    let phase: 'request' | 'invoke' = 'request';
    try {
      signal?.throwIfAborted();
      const parsed = sdkRequestSchema.safeParse(await readBody(request, limits.bodyBytes));
      signal?.throwIfAborted();
      if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
      if (!parsed.success) throw new Error(`Unsupported Messages payload: ${parsed.error.issues.map(issue => `${issue.path.join('.')}:${issue.code}`).join(', ')}`);
      // The owner chooses the model (Claude Code `/model`, Codex `-c model=`): a Dev Box host forwards the
      // request's model as named and the gateway applies the owner's entitlement, exactly as from a laptop
      // (dev 2026-10-25). The run's model is only the default (ANTHROPIC_MODEL); the chat sandbox keeps its pin.
      const requestedModel = limits.cacheMarkers ? parsed.data.model : modelId;
      if (!limits.cacheMarkers && parsed.data.model !== modelId && parsed.data.model !== wireModelId) {
        throw new Error(`Unsupported Messages model: this sandbox serves ${modelId}`);
      }
      if (!/^[A-Za-z0-9_.:-]{1,256}$/.test(requestedModel)) throw new Error('Unsupported Messages model');
      const { model: _model, stream, temperature: _temperature, system, messages,
        metadata: _metadata, thinking: _thinking, context_management: _management, output_config: _output, ...data } = parsed.data;
      // #9024: the Bedrock body carries no `metadata`; the gateway learns the Claude Code session from a header instead.
      const store = hostCallClient.getStore();
      if (store && typeof _metadata?.user_id === 'string') store.session = clientUserAgentHeader(_metadata.user_id);
      const reminders = messages.filter(message => message.role === 'system');
      // A host forwards the system prompt and Claude Code's `system`-role reminders exactly as sent: the
      // trailing reminder carries the cache breakpoint that caches the whole history, and the gateway
      // keeps it in conversation order (#8223 cause 51). The sandbox flattens system text and reminders
      // into the one string its contract allows (and has no cache markers to lose).
      const systemBlocks: SystemBlock[] = [
        ...(system === undefined ? [] : typeof system === 'string' ? [{ type: 'text' as const, text: system }] : system),
        ...(limits.cacheMarkers ? [] : reminders.flatMap(message => typeof message.content === 'string'
          ? [{ type: 'text' as const, text: message.content }] : message.content)),
      ];
      const systemForGateway: ChatModelRequest['system'] | undefined = limits.cacheMarkers ? system
        : !systemBlocks.length ? undefined : systemBlocks.map(block => block.text).filter(Boolean).join('\n');
      const conversation = limits.cacheMarkers ? messages : messages.filter(message => message.role !== 'system');
      const modelRequest: ChatModelRequest = {
        ...data, messages: conversation.map(message => message.role === 'system' ? { role: message.role, content: message.content } : ({
          // `output_config` on a message is never forwarded; `role` and `content` are the contract.
          role: message.role, content: typeof message.content === 'string' ? message.content : message.content.map(block =>
            block.type === 'tool_result' && Array.isArray(block.content)
              ? { ...block, content: block.content.map(item => item.text).join('\n') } : block),
        })) as ChatModelRequest['messages'],
        max_tokens: Math.min(data.max_tokens, limits.maxTokens),
        ...(systemForGateway === undefined ? {} : { system: systemForGateway }),
      };
      if (!outgoingSchema.safeParse(modelRequest).success) throw new Error('Unsupported Messages content');
      gatewayError = undefined;
      phase = 'invoke';
      if (stream && typeof client.invokeModelStream === 'function') {
        await relayStream(client, requestedModel, modelRequest, request, response, signal, () => current !== scope || scopeCancellation.signal.aborted,
          limits.cacheMarkers);
        return;
      }
      // The trailing model argument is a host-profile extension of the client contract; the sandbox client
      // has no such parameter and its calls keep the four-argument shape.
      const result = limits.cacheMarkers
        ? await (client.invokeModel as (...args: unknown[]) => ReturnType<ChatDataClient['invokeModel']>)(
          `sdk_${randomBytes(12).toString('hex')}`, modelRequest, undefined, deliverResponse, undefined, requestedModel)
        : await client.invokeModel(`sdk_${randomBytes(12).toString('hex')}`, modelRequest, undefined, deliverResponse);
      if (result.modelId !== requestedModel) throw new ChatDataError('scope_mismatch');
      signal?.throwIfAborted();
      if (current !== scope || scopeCancellation.signal.aborted) throw new ChatDataError('scope_mismatch');
      const message = { id: `msg_${randomBytes(12).toString('hex')}`, type: 'message', role: 'assistant',
        model: result.modelId, content: result.content, stop_reason: result.stopReason, stop_sequence: null,
        usage: { input_tokens: result.usage.input_tokens, output_tokens: result.usage.output_tokens } };
      if (!stream) {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify(message));
        return;
      }
      response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      const event = (name: string, data: unknown) => response.write(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
      event('message_start', { type: 'message_start', message: { ...message, content: [], stop_reason: null,
        usage: { input_tokens: message.usage.input_tokens, output_tokens: 0 } } });
      for (const [index, block] of result.content.entries()) {
        const initial = block.type === 'text' ? { type: 'text', text: '' } :
          block.type === 'tool_use' ? { type: 'tool_use', id: block.id, name: block.name, input: {} } :
            { type: 'thinking', thinking: '', signature: '' };
        event('content_block_start', { type: 'content_block_start', index, content_block: initial });
        if (block.type === 'text') event('content_block_delta', { type: 'content_block_delta', index,
          delta: { type: 'text_delta', text: block.text } });
        if (block.type === 'tool_use') event('content_block_delta', { type: 'content_block_delta', index,
          delta: { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } });
        if (block.type === 'thinking') {
          event('content_block_delta', { type: 'content_block_delta', index, delta: { type: 'thinking_delta', thinking: block.thinking } });
          event('content_block_delta', { type: 'content_block_delta', index, delta: { type: 'signature_delta', signature: block.signature } });
        }
        event('content_block_stop', { type: 'content_block_stop', index });
      }
      event('message_delta', { type: 'message_delta', delta: { stop_reason: result.stopReason, stop_sequence: null },
        usage: { output_tokens: message.usage.output_tokens } });
      event('message_stop', { type: 'message_stop' });
      response.end();
    } catch (error) {
      if (current === scope && error instanceof ChatDataError) gatewayError = error;
      const failure = sdkFailure(error, phase);
      // Request-shape refusals are logged too: a silent 400 here made Claude Code retry each turn with a
      // different, larger body and nobody could see why (#8223 cause 50).
      logFailure('messages', error, failure, startedAt, report);
      if (response.headersSent) response.destroy(error as Error);
      else respond(response, failure.status, failure.message, failure.type, failure.headers);
    }
  };
  // #9024: every host call made while serving this request names the harness that made it.
  const server = createServer((request, response) => hostCallClient.run({ userAgent: clientUserAgentHeader(request.headers['user-agent']) },
    () => handleRequest(request, response)));
  // No connection cap: Claude Code's keep-alive chat socket, Kiro Crew's own title/model calls and the
  // adp-mcp.py tools shim legitimately run concurrently; a cap of 2 reset the third connection, which
  // Claude Code reported as "Network error. Please check your internet connection." (#8223 cause 41).
  // Every request is authenticated with the per-boot secret and the server binds to 127.0.0.1 only.
  server.requestTimeout = 10_000;
  server.headersTimeout = 10_000;
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return { baseUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, secret,
    get gatewayError() { return gatewayError; },
    bindTurn,
    /** Fill the tools catalogue for the current scope before anyone asks (#8835); resolves to the gateway's status. */
    warmTools: async () => (await fetchListing(scope)).status,
    suspend: () => { scopeCancellation.abort(); githubToken = undefined; listing = undefined; listingInFlight = undefined; },
    close: () => {
      scopeCancellation.abort();
      githubToken = undefined;
      listing = undefined;
      listingInFlight = undefined;
      return new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    } };
}
