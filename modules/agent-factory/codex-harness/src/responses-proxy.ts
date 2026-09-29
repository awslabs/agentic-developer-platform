import http from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { z } from "zod";
import type { HostTool } from "./tool-server.js";

const textPart = z.strictObject({ type: z.enum(["input_text", "output_text"]), text: z.string(), annotations: z.array(z.never()).optional() });
const message = z.strictObject({
  type: z.literal("message").optional(), role: z.enum(["system", "developer", "user", "assistant"]),
  content: z.union([z.string(), z.array(textPart).max(64)]),
  phase: z.enum(["commentary", "final_answer"]).optional(),
  id: z.string().max(200).optional(), status: z.literal("completed").optional(),
  internal_chat_message_metadata_passthrough: z.unknown().optional(),
}).transform(({ internal_chat_message_metadata_passthrough: _discarded, id: _discardedId, ...item }) => item);
const reasoningFields = {
  type: z.literal("reasoning"),
  encrypted_content: z.string().min(1).max(32768),
  summary: z.array(z.strictObject({ type: z.literal("summary_text"), text: z.string().max(32000) })).max(16),
  status: z.literal("completed").optional(),
};
const reasoningInput = z.strictObject({ ...reasoningFields, id: z.string().max(200).optional(),
  content: z.null().optional(), internal_chat_message_metadata_passthrough: z.unknown().optional(),
}).transform(({ id: _discardedId, content: _emptyContent, internal_chat_message_metadata_passthrough: _metadata, ...item }) => item);
const reasoningOutput = z.strictObject({ ...reasoningFields, id: z.string().min(1).max(200) });
const functionFields = {
  type: z.literal("function_call"), call_id: z.string().min(1).max(200),
  namespace: z.literal("mcp__adp"), name: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/),
  arguments: z.string().min(2).max(32768), status: z.literal("completed").optional(),
};
const functionInput = z.strictObject({ ...functionFields, id: z.string().max(200).optional(),
  internal_chat_message_metadata_passthrough: z.unknown().optional() })
  .transform(({ id: _discarded, internal_chat_message_metadata_passthrough: _metadata, ...item }) => item);
const functionOutput = z.strictObject({ ...functionFields, id: z.string().min(1).max(200) });
const functionResult = z.strictObject({ id: z.string().max(200).optional(),
  internal_chat_message_metadata_passthrough: z.unknown().optional(), type: z.literal("function_call_output"), call_id: z.string().min(1).max(200),
  output: z.union([z.string().max(32768), z.array(z.strictObject({ type: z.literal("input_text"), text: z.string().max(32768) })).max(16)]),
}).transform(({ id: _discarded, internal_chat_message_metadata_passthrough: _metadata, ...item }) => item);
export type ToolHistory = z.infer<typeof functionInput> | z.infer<typeof functionResult>;
export interface ResponsesTools {
  /** Reviewed host schemas, never SDK- or persona-supplied authority. */
  definitions: readonly HostTool[];
  /** Must compare every call/result with this run's confirmed model/tool
   * receipts. Structural call-ID pairing is not evidence authenticity. */
  validateHistory(history: readonly ToolHistory[]): true;
  /** Commit session call binding only after the bridge validates output/usage. */
  acceptResponse?(response: TextResponsesResult): true;
}
const sdkRequest = z.strictObject({
  model: z.string(), input: z.union([z.string().min(1), z.array(z.union([message, reasoningInput, functionInput, functionResult])).min(1).max(64)]),
  instructions: z.string().optional(), stream: z.literal(true), store: z.literal(false),
  reasoning: z.strictObject({ effort: z.enum(["minimal", "low", "medium", "high", "xhigh"]), summary: z.enum(["auto", "concise", "detailed", "none"]).optional() }),
  // These are SDK-owned transport hints, never authority or cross-run cache IDs.
  client_metadata: z.record(z.string(), z.string()).optional(), prompt_cache_key: z.string().optional(),
  include: z.array(z.literal("reasoning.encrypted_content")).max(1).optional(),
  parallel_tool_calls: z.boolean().optional(), tool_choice: z.literal("auto").optional(),
  tools: z.array(z.unknown()).max(64).optional(),
  max_output_tokens: z.number().int().positive().optional(),
});

export interface TextResponsesPolicy {
  model: string;
  effort: "minimal" | "low" | "medium" | "high" | "xhigh";
  maxOutputTokens: number;
  maxRequestBytes: number;
  maxResponseBytes: number;
  maxOperations: number;
  timeoutMs: number;
  tools?: ResponsesTools;
}
export interface TextResponsesRequest {
  input: z.infer<typeof sdkRequest>["input"];
  instructions?: string;
  reasoning: { effort: TextResponsesPolicy["effort"] };
  max_output_tokens: number;
  tools?: object[];
  parallel_tool_calls?: false;
}
const usageSchema = z.strictObject({
  input_tokens: z.number().int().nonnegative().safe(), output_tokens: z.number().int().nonnegative().safe(),
  input_tokens_details: z.strictObject({ cached_tokens: z.number().int().nonnegative().safe(), cache_write_tokens: z.number().int().nonnegative().safe().optional() }).optional(),
  output_tokens_details: z.strictObject({ reasoning_tokens: z.number().int().nonnegative().safe() }).optional(),
});
const responseSchema = z.strictObject({
  id: z.string().min(1).max(200), status: z.literal("completed"),
  output: z.array(z.union([reasoningOutput, functionOutput, z.strictObject({
    id: z.string().min(1).max(200), type: z.literal("message"), role: z.literal("assistant"), status: z.literal("completed"),
    phase: z.enum(["commentary", "final_answer"]).optional(),
    content: z.array(z.strictObject({ type: z.literal("output_text"), text: z.string(), annotations: z.array(z.never()) })).min(1).max(64),
  })])).min(1).max(16), usage: usageSchema,
});
export type TextResponsesResult = z.infer<typeof responseSchema>;
export interface ConfirmedTextOperation {
  operationStatus: "confirmed";
  response: TextResponsesResult;
}
/** The host callback owns authentication, model selection, reservations and
 * durable handoff/settlement. A resolved promise must mean a confirmed receipt.
 * This module holds no gateway, repository, AWS or provider credential.
 */
export type TextResponsesHost = (request: TextResponsesRequest, signal: AbortSignal) => Promise<ConfirmedTextOperation>;

function boundedMessage(value: z.infer<typeof message>): z.infer<typeof message> {
  const parts = typeof value.content === "string"
    ? [{ type: value.role === "assistant" ? "output_text" as const : "input_text" as const, text: value.content }]
    : value.content;
  if (parts.every(part => part.text.length <= 32000)) return value;
  const content = parts.flatMap(part => {
    if (part.text.length <= 32000) return [part];
    const points = Array.from(part.text);
    const chunks = [];
    for (let offset = 0; offset < points.length; offset += 16000)
      chunks.push({ ...part, text: points.slice(offset, offset + 16000).join("") });
    return chunks;
  });
  if (content.length > 64) throw new Error("Responses message exceeds part bound");
  return { ...value, content };
}

export function normalizeTextRequest(value: unknown, policy: TextResponsesPolicy): TextResponsesRequest {
  const parsed = sdkRequest.parse(value);
  if (parsed.model !== policy.model || parsed.reasoning.effort !== policy.effort) throw new Error("SDK model binding mismatch");
  const admitted = new Map((policy.tools?.definitions ?? []).map(tool => [tool.name, tool]));
  let namespaceSeen = false;
  for (const declaration of parsed.tools ?? []) {
    const kind = z.object({ type: z.string() }).parse(declaration);
    if (kind.type === "namespace" && policy.tools) {
      const namespace = z.object({ type: z.literal("namespace"), name: z.literal("mcp__adp"),
        tools: z.array(z.object({ type: z.literal("function"), name: z.string() })).max(64) }).parse(declaration);
      if (namespaceSeen || namespace.tools.length !== admitted.size
        || new Set(namespace.tools.map(tool => tool.name)).size !== admitted.size
        || namespace.tools.some(tool => !admitted.has(tool.name))) throw new Error("SDK tool catalogue mismatch");
      namespaceSeen = true;
    } else {
      const residual = policy.tools
        ? ["view_image", "request_user_input", "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"]
        : ["view_image", "request_user_input"];
      const tool = z.object({ type: z.literal("function"), name: z.string() }).parse(declaration);
      if (!residual.includes(tool.name)) throw new Error("SDK tool not admitted");
    }
  }
  if (policy.tools && (!admitted.size || !namespaceSeen)) throw new Error("Missing host tool namespace");
  const history = typeof parsed.input === "string" ? [] : parsed.input.filter(
    (item): item is ToolHistory => item.type === "function_call" || item.type === "function_call_output");
  if (history.length && !policy.tools) throw new Error("Executable history not admitted");
  const pending = new Set<string>(), completed = new Set<string>();
  for (const item of history) {
    if (item.type === "function_call") {
      if (pending.size || completed.has(item.call_id)) throw new Error("Overlapping or duplicate tool call");
      validateToolCall(item, policy);
      pending.add(item.call_id);
    } else {
      if (!pending.delete(item.call_id)) throw new Error("Unmatched tool result");
      completed.add(item.call_id);
    }
  }
  if (pending.size) throw new Error("Unresolved tool call");
  if (policy.tools && policy.tools.validateHistory(history) !== true) throw new Error("Tool history not verified");
  const normalized: TextResponsesRequest = {
    ...(policy.tools ? { tools: [{ type: "namespace", name: "mcp__adp", description: "Authorized ADP tools.",
      tools: policy.tools.definitions.map(tool => ({ type: "function", name: tool.name, description: tool.description,
        parameters: tool.parameters ? structuredClone(tool.parameters) : z.toJSONSchema(tool.input.strict(), { target: "draft-7" }), strict: false })) }], parallel_tool_calls: false as const } : {}),
    input: Array.isArray(parsed.input) ? parsed.input.map(item => "role" in item ? boundedMessage(item) : item) : parsed.input, ...(parsed.instructions === undefined ? {} : { instructions: parsed.instructions }),
    reasoning: { effort: policy.effort },
    max_output_tokens: Math.min(parsed.max_output_tokens ?? policy.maxOutputTokens, policy.maxOutputTokens),
  };
  if (Buffer.byteLength(JSON.stringify(normalized)) > policy.maxRequestBytes) throw new Error("Responses request exceeds bound");
  return normalized;
}

function validateToolCall(call: z.infer<typeof functionInput>, policy: TextResponsesPolicy) {
  const tool = policy.tools?.definitions.find(tool => tool.name === call.name);
  if (!tool) throw new Error("Response tool not admitted");
  tool.input.strict().parse(JSON.parse(call.arguments));
}

/** Strip native provider envelope metadata and documented empty placeholders.
 * Execution fields and usage retain the closed contract below.
 */
function normalizeProviderResult(value: unknown): unknown {
  const envelope = z.object({ id: z.unknown(), status: z.unknown(), output: z.unknown(), usage: z.unknown() }).parse(value);
  const result = structuredClone(envelope);
  if (result.usage && typeof result.usage === "object" && !Array.isArray(result.usage)) {
    const usage = result.usage as Record<string, unknown>;
    if ("total_tokens" in usage) {
      if (!Number.isSafeInteger(usage.total_tokens) || typeof usage.input_tokens !== "number"
        || typeof usage.output_tokens !== "number" || usage.total_tokens !== usage.input_tokens + usage.output_tokens)
        throw new Error("Provider total usage differs");
      delete usage.total_tokens;
    }
  }
  if (Array.isArray(result.output)) for (const item of result.output) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    if (item.type === "reasoning" && Array.isArray(item.content) && item.content.length === 0) delete item.content;
    if (item.type === "message" && Array.isArray(item.content)) for (const part of item.content) {
      if (part && typeof part === "object" && Array.isArray(part.logprobs) && part.logprobs.length === 0) delete part.logprobs;
    }
  }
  return result;
}

export function textResponseEvents(value: unknown, policy: TextResponsesPolicy): string {
  if (Buffer.byteLength(JSON.stringify(value)) > policy.maxResponseBytes) throw new Error("Responses result exceeds bound");
  const response = responseSchema.parse(normalizeProviderResult(value));
  if (!Number.isSafeInteger(response.usage.input_tokens + response.usage.output_tokens)
    || response.usage.output_tokens > policy.maxOutputTokens
    || ((response.usage.input_tokens_details?.cached_tokens ?? 0) + (response.usage.input_tokens_details?.cache_write_tokens ?? 0)) > response.usage.input_tokens
    || (response.usage.output_tokens_details?.reasoning_tokens ?? 0) > response.usage.output_tokens) throw new Error("Invalid Responses usage");
  const calls = response.output.filter(item => item.type === "function_call");
  if (calls.length > 1) throw new Error("Parallel tool calls not admitted");
  for (const call of calls) validateToolCall(call, policy);
  const events: string[] = [];
  const emit = (type: string, fields: object) => events.push(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
  emit("response.created", { response: { id: response.id, object: "response", status: "in_progress", output: [] } });
  response.output.forEach((item, output_index) => {
    if (item.type === "function_call") {
      emit("response.output_item.added", { output_index, item: { ...item, status: "in_progress", arguments: "" } });
      emit("response.function_call_arguments.delta", { output_index, item_id: item.id, delta: item.arguments });
      emit("response.function_call_arguments.done", { output_index, item_id: item.id, arguments: item.arguments });
      emit("response.output_item.done", { output_index, item });
      return;
    }
    if (item.type === "reasoning") {
      // Reasoning is replayable protocol state, never a progress/log message.
      emit("response.output_item.added", { output_index, item: { ...item, summary: [] } });
      emit("response.output_item.done", { output_index, item });
      return;
    }
    emit("response.output_item.added", { output_index, item: { ...item, status: "in_progress", content: [] } });
    item.content.forEach((part, content_index) => {
      const position = { item_id: item.id, output_index, content_index };
      emit("response.content_part.added", { ...position, part: { ...part, text: "" } });
      emit("response.output_text.delta", { ...position, delta: part.text });
      emit("response.output_text.done", { ...position, text: part.text });
      emit("response.content_part.done", { ...position, part });
    });
    emit("response.output_item.done", { output_index, item });
  });
  emit("response.completed", { response: { ...response, object: "response", usage: { ...response.usage, total_tokens: response.usage.input_tokens + response.usage.output_tokens } } });
  return events.join("");
}

export class ResponsesBridgeError extends Error {
  constructor(readonly code: "request_bound_exceeded" | "sdk_envelope_bound_exceeded" | "request_contract_invalid" | "handoff_or_response_failed") {
    super(`Codex Responses bridge: ${code}`);
  }
}

/** Bounded Responses transport; tools require explicit host schemas and receipt
 * validation. No execution, Task lifecycle or safe-resume authority lives here.
 * Once a model outcome is ambiguous, further SDK calls are refused locally.
 */
export async function startTextResponsesProxy(host: TextResponsesHost, policy: TextResponsesPolicy) {
  if (!policy.model.trim() || !["minimal", "low", "medium", "high", "xhigh"].includes(policy.effort)) throw new Error("Invalid Responses policy");
  for (const value of [policy.maxOutputTokens, policy.maxRequestBytes, policy.maxResponseBytes, policy.maxOperations, policy.timeoutMs]) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("Invalid Responses limit");
  }
  // A fresh immutable host snapshot prevents caller mutation during a request.
  if (policy.tools && (typeof policy.tools.validateHistory !== "function" || policy.tools.definitions.length > 64
    || new Set(policy.tools.definitions.map(tool => tool.name)).size !== policy.tools.definitions.length)) throw new Error("Invalid host tool policy");
  policy = Object.freeze({ ...policy, ...(policy.tools ? { tools: Object.freeze({ ...policy.tools,
    definitions: Object.freeze(policy.tools.definitions.map(tool => Object.freeze({ ...tool, ...(tool.parameters ? { parameters: structuredClone(tool.parameters) } : {}), input: tool.input.strict() }))) }) } : {}) });
  const token = randomBytes(32).toString("hex");
  const expected = Buffer.from(`Bearer ${token}`);
  let failed = false;
  let failure: ResponsesBridgeError | undefined;
  let busy = false;
  let operations = 0;
  let active: AbortController | undefined;
  const server = http.createServer(async (req, res) => {
    const header = Buffer.from(req.headers.authorization ?? "");
    if (header.length !== expected.length || !timingSafeEqual(header, expected)) { res.writeHead(401).end(); return; }
    if (req.method === "GET" && req.url === "/v1/responses") {
      // SDK WebSocket negotiation falls back to the admitted HTTP contract.
      res.writeHead(426, { "content-type": "application/json", "content-length": "2" }).end("{}"); return;
    }
    if (req.method !== "POST" || req.url !== "/v1/responses") { res.writeHead(404).end(); return; }
    if (failed || busy || operations >= policy.maxOperations) { res.writeHead(409).end(); return; }
    busy = true;
    let dispatched = false;
    const controller = new AbortController();
    active = controller;
    const timer = setTimeout(() => controller.abort(new Error("Responses deadline elapsed")), policy.timeoutMs);
    let rejectAbort: (reason: unknown) => void = () => {};
    const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject; });
    // Attach a handler immediately: the body can time out before host dispatch.
    void aborted.catch(() => {});
    const abort = () => { rejectAbort(controller.signal.reason); req.destroy(); };
    controller.signal.addEventListener("abort", abort, { once: true });
    const disconnect = () => { if (!res.writableEnded) controller.abort(new Error("SDK disconnected")); };
    res.once("close", disconnect);
    try {
      const chunks: Buffer[] = [];
      let bytes = 0;
      for await (const chunk of req) {
        bytes += chunk.length;
        // SDK metadata is removed before the unchanged gateway/IPC request bound.
        if (bytes > Math.min(256 * 1024, policy.maxRequestBytes * 4)) throw new Error("Responses HTTP body exceeds bound");
        chunks.push(chunk);
      }
      controller.signal.throwIfAborted();
      const request = normalizeTextRequest(JSON.parse(Buffer.concat(chunks).toString("utf8")), policy);
      operations++;
      dispatched = true;
      const receipt = await Promise.race([host(request, controller.signal), aborted]);
      controller.signal.throwIfAborted();
      if (receipt.operationStatus !== "confirmed") throw new Error("Unconfirmed model operation");
      const previousCalls = new Set(typeof request.input === "string" ? [] : request.input
        .filter(item => item.type === "function_call").map(item => item.call_id));
      if (receipt.response.output.some(item => item.type === "function_call" && previousCalls.has(item.call_id))) {
        throw new Error("Provider repeated a completed tool call");
      }
      const output = textResponseEvents(receipt.response, { ...policy, maxOutputTokens: request.max_output_tokens });
      if (policy.tools?.acceptResponse && policy.tools.acceptResponse(receipt.response) !== true) throw new Error("Tool response binding refused");
      res.writeHead(200, { "content-type": "text/event-stream" }).end(output);
    } catch (error) {
      const bound = error instanceof Error && ["Responses HTTP body exceeds bound", "Responses request exceeds bound"].includes(error.message);
      failure = new ResponsesBridgeError(dispatched ? "handoff_or_response_failed" : error instanceof Error && error.message === "Responses HTTP body exceeds bound" ? "sdk_envelope_bound_exceeded" : bound ? "request_bound_exceeded" : "request_contract_invalid");
      // No automatic replay after possible handoff or malformed input. Arbitrary
      // provider/host errors never reach SDK messages, logs or telemetry.
      failed = true;
      if (!res.headersSent) res.writeHead(dispatched ? 502 : 400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { code: "task_responses_refused", message: "Task host did not confirm this Responses operation." } }));
    } finally {
      clearTimeout(timer);
      res.removeListener("close", disconnect);
      controller.signal.removeEventListener("abort", abort);
      busy = false;
      active = undefined;
    }
  });
  server.requestTimeout = Math.min(policy.timeoutMs, 30000);
  server.headersTimeout = Math.min(policy.timeoutMs, 10000);
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Responses listener unavailable");
  return { baseUrl: `http://127.0.0.1:${address.port}/v1`, token,
    get failure() { return failure; },
    close: async () => {
      failed = true;
      active?.abort(new Error("Responses proxy closed"));
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
    },
  };
}
