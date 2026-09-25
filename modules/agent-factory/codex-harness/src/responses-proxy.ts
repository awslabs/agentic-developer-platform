import http from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { z } from "zod";

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
const sdkRequest = z.strictObject({
  model: z.string(), input: z.union([z.string().min(1), z.array(z.union([message, reasoningInput])).min(1).max(64)]),
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
}
export interface TextResponsesRequest {
  input: z.infer<typeof sdkRequest>["input"];
  instructions?: string;
  reasoning: { effort: TextResponsesPolicy["effort"] };
  max_output_tokens: number;
}
const usageSchema = z.strictObject({
  input_tokens: z.number().int().nonnegative().safe(), output_tokens: z.number().int().nonnegative().safe(),
  input_tokens_details: z.strictObject({ cached_tokens: z.number().int().nonnegative().safe() }).optional(),
  output_tokens_details: z.strictObject({ reasoning_tokens: z.number().int().nonnegative().safe() }).optional(),
});
const responseSchema = z.strictObject({
  id: z.string().min(1).max(200), status: z.literal("completed"),
  output: z.array(z.union([reasoningOutput, z.strictObject({
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

export function normalizeTextRequest(value: unknown, policy: TextResponsesPolicy): TextResponsesRequest {
  const parsed = sdkRequest.parse(value);
  if (parsed.model !== policy.model || parsed.reasoning.effort !== policy.effort) throw new Error("SDK model binding mismatch");
  // Native residual tools are not projected into this text-only contract. Reject
  // all other declarations so a new SDK tool cannot acquire implicit authority.
  for (const tool of parsed.tools ?? []) {
    z.object({ type: z.literal("function"), name: z.enum(["view_image", "request_user_input"]) }).parse(tool);
  }
  const normalized: TextResponsesRequest = {
    input: parsed.input, ...(parsed.instructions === undefined ? {} : { instructions: parsed.instructions }),
    reasoning: { effort: policy.effort },
    max_output_tokens: Math.min(parsed.max_output_tokens ?? policy.maxOutputTokens, policy.maxOutputTokens),
  };
  if (Buffer.byteLength(JSON.stringify(normalized)) > policy.maxRequestBytes) throw new Error("Responses request exceeds bound");
  return normalized;
}

export function textResponseEvents(value: unknown, policy: TextResponsesPolicy): string {
  if (Buffer.byteLength(JSON.stringify(value)) > policy.maxResponseBytes) throw new Error("Responses result exceeds bound");
  const response = responseSchema.parse(value);
  if (!Number.isSafeInteger(response.usage.input_tokens + response.usage.output_tokens)
    || response.usage.output_tokens > policy.maxOutputTokens
    || (response.usage.input_tokens_details?.cached_tokens ?? 0) > response.usage.input_tokens
    || (response.usage.output_tokens_details?.reasoning_tokens ?? 0) > response.usage.output_tokens) throw new Error("Invalid Responses usage");
  const events: string[] = [];
  const emit = (type: string, fields: object) => events.push(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
  emit("response.created", { response: { id: response.id, object: "response", status: "in_progress", output: [] } });
  response.output.forEach((item, output_index) => {
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

/** Text-only transport milestone. No tool execution, session isolation, Task
 * lifecycle or safe-resume authority is supplied by this loopback adapter.
 * Once a model outcome is ambiguous, further SDK calls are refused locally.
 */
export async function startTextResponsesProxy(host: TextResponsesHost, policy: TextResponsesPolicy) {
  if (!policy.model.trim() || !["minimal", "low", "medium", "high", "xhigh"].includes(policy.effort)) throw new Error("Invalid Responses policy");
  for (const value of [policy.maxOutputTokens, policy.maxRequestBytes, policy.maxResponseBytes, policy.maxOperations, policy.timeoutMs]) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("Invalid Responses limit");
  }
  // A fresh immutable host snapshot prevents caller mutation during a request.
  policy = Object.freeze({ ...policy });
  const token = randomBytes(32).toString("hex");
  const expected = Buffer.from(`Bearer ${token}`);
  let failed = false;
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
        if (bytes > policy.maxRequestBytes) throw new Error("Responses HTTP body exceeds bound");
        chunks.push(chunk);
      }
      controller.signal.throwIfAborted();
      const request = normalizeTextRequest(JSON.parse(Buffer.concat(chunks).toString("utf8")), policy);
      operations++;
      dispatched = true;
      const receipt = await Promise.race([host(request, controller.signal), aborted]);
      controller.signal.throwIfAborted();
      if (receipt.operationStatus !== "confirmed") throw new Error("Unconfirmed model operation");
      const output = textResponseEvents(receipt.response, { ...policy, maxOutputTokens: request.max_output_tokens });
      res.writeHead(200, { "content-type": "text/event-stream" }).end(output);
    } catch {
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
    close: async () => {
      failed = true;
      active?.abort(new Error("Responses proxy closed"));
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
    },
  };
}
