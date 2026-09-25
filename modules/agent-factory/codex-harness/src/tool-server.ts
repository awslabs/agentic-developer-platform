import http from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { z } from "zod";
import type { Capability } from "./persona.js";

/** Reviewed host code supplies schemas; persona JSON never supplies handlers. */
export interface HostTool {
  name: string;
  description: string;
  capability: Capability;
  input: z.ZodObject;
  readOnly: boolean;
}
export interface ToolHost {
  assertCurrent(signal: AbortSignal): Promise<void>;
  /** Reauthorize and journal effects in the gateway. MCP IDs confer no authority.
   * An ambiguous effect must reject, never return a confirmed error receipt. */
  execute(tool: string, args: Record<string, unknown>, signal: AbortSignal): Promise<{
    status: "confirmed"; content: string; isError?: boolean;
  }>;
}
export interface ToolServerPolicy {
  capabilities: readonly Capability[];
  maxCalls: number;
  maxRequestBytes: number;
  maxResultBytes: number;
  timeoutMs: number;
  signal: AbortSignal;
}
const envelope = z.strictObject({
  jsonrpc: z.literal("2.0"), id: z.union([z.string().min(1).max(128), z.number().int().safe()]).optional(),
  method: z.string().min(1).max(64), params: z.record(z.string(), z.unknown()).optional(),
});
const callSchema = z.strictObject({ name: z.string(), arguments: z.record(z.string(), z.unknown()),
  _meta: z.record(z.string(), z.unknown()).optional() });
const receiptSchema = z.strictObject({ status: z.literal("confirmed"), content: z.string(), isError: z.boolean().optional() });

/** Run-local MCP transport. No repository/model/AWS credentials live here.
 * This restricts the advertised catalogue and bounds SDK traffic. Privileged
 * execution and durable reconciliation remain the host/gateway's responsibility. */
export async function startToolServer(tools: readonly HostTool[], host: ToolHost, policy: ToolServerPolicy) {
  for (const value of [policy.maxCalls, policy.maxRequestBytes, policy.maxResultBytes, policy.timeoutMs]) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("Invalid tool server bound");
  }
  if (tools.length > 64) throw new Error("Tool catalogue exceeds bound");
  const catalogue = new Map<string, { definition: object; input: z.ZodObject }>();
  for (const tool of tools) {
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(tool.name) || catalogue.has(tool.name)
      || !tool.description.trim() || tool.description.length > 4096) throw new Error("Invalid host tool definition");
    if (!policy.capabilities.includes(tool.capability)) throw new Error("Tool capability not admitted");
    const input = tool.input.strict();
    catalogue.set(tool.name, { input, definition: {
      name: tool.name, description: tool.description,
      inputSchema: z.toJSONSchema(input, { target: "draft-7" }),
      annotations: { readOnlyHint: tool.readOnly, destructiveHint: !tool.readOnly, openWorldHint: false },
    } });
  }
  const definitions = [...catalogue.values()].map(tool => tool.definition);
  if (Buffer.byteLength(JSON.stringify(definitions)) > policy.maxResultBytes) throw new Error("Tool catalogue exceeds result bound");
  policy = Object.freeze({ ...policy, capabilities: Object.freeze([...policy.capabilities]) });
  const token = randomBytes(32).toString("hex");
  const expected = Buffer.from(`Bearer ${token}`);
  const lifetime = new AbortController();
  let busy = false, failed = false, calls = 0;
  const seen = new Set<string>();
  const server = http.createServer(async (req, res) => {
    const auth = Buffer.from(req.headers.authorization ?? "");
    if (auth.length !== expected.length || !timingSafeEqual(auth, expected)) { res.writeHead(401).end(); return; }
    if (req.url !== "/mcp") { res.writeHead(404).end(); return; }
    if (req.method !== "POST") { res.writeHead(405).end(); return; }
    if (busy || failed || lifetime.signal.aborted || policy.signal.aborted) { res.writeHead(409).end(); return; }
    busy = true;
    const active = new AbortController();
    const signal = AbortSignal.any([active.signal, lifetime.signal, policy.signal, AbortSignal.timeout(policy.timeoutMs)]);
    const disconnect = () => { if (!res.writableEnded) active.abort(new Error("Tool client disconnected")); };
    res.once("close", disconnect);
    const abortBody = () => req.destroy();
    signal.addEventListener("abort", abortBody, { once: true });
    let id: string | number | undefined;
    let handedOff = false;
    let rejectAbort: (reason: unknown) => void = () => {};
    const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject; });
    void aborted.catch(() => {});
    const onAbort = () => rejectAbort(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    try {
      const parts: Buffer[] = []; let size = 0;
      for await (const part of req) {
        size += part.length;
        if (size > policy.maxRequestBytes) throw new Error("Tool request exceeds bound");
        parts.push(part);
      }
      signal.throwIfAborted();
      const body = envelope.parse(JSON.parse(Buffer.concat(parts).toString("utf8")));
      id = body.id;
      if (id === undefined) {
        if (body.method !== "notifications/initialized") throw new Error("Unsupported notification");
        res.writeHead(202).end(); return;
      }
      // Correlation is not idempotency. Refuse repeated transport IDs instead of
      // replaying a mutation or returning sensitive historical results.
      const key = JSON.stringify(id);
      if (seen.has(key) || seen.size >= policy.maxCalls + 128) throw new Error("Duplicate or excessive MCP request");
      seen.add(key);
      await Promise.race([host.assertCurrent(signal), aborted]);
      signal.throwIfAborted();
      let result: object;
      if (body.method === "initialize") {
        const params = z.object({ protocolVersion: z.enum(["2024-11-05", "2025-03-26", "2025-06-18"]) }).parse(body.params);
        result = { protocolVersion: params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: "adp", version: "1" } };
      } else if (body.method === "tools/list") {
        if (body.params && Object.keys(body.params).some(key => key !== "_meta")) throw new Error("Unsupported catalogue request");
        result = { tools: definitions };
      } else if (body.method === "ping") result = {};
      else if (body.method === "tools/call") {
        const call = callSchema.parse(body.params);
        const tool = catalogue.get(call.name);
        if (!tool || calls >= policy.maxCalls) throw new Error("Tool not admitted or exhausted");
        const args = tool.input.parse(call.arguments);
        calls++;
        handedOff = true;
        const receipt = receiptSchema.parse(await Promise.race([host.execute(call.name, args, signal), aborted]));
        signal.throwIfAborted();
        if (Buffer.byteLength(receipt.content) > policy.maxResultBytes) throw new Error("Tool result exceeds bound");
        await Promise.race([host.assertCurrent(signal), aborted]);
        signal.throwIfAborted();
        result = { content: [{ type: "text", text: receipt.content }], isError: receipt.isError ?? false };
      } else throw new Error("Unsupported MCP method");
      const output = JSON.stringify({ jsonrpc: "2.0", id, result });
      if (Buffer.byteLength(output) > policy.maxResultBytes) throw new Error("MCP response exceeds bound");
      res.writeHead(200, { "content-type": "application/json" }).end(output);
    } catch {
      // Never replay an unknown outcome or leak host exception/credential text.
      if (handedOff) failed = true;
      if (!res.destroyed) res.writeHead(id === undefined ? 400 : 200, { "content-type": "application/json" }).end(JSON.stringify({
        jsonrpc: "2.0", id: id ?? null, error: { code: -32000, message: "ADP tool request refused" },
      }));
    } finally {
      signal.removeEventListener("abort", abortBody);
      signal.removeEventListener("abort", onAbort);
      res.removeListener("close", disconnect);
      busy = false;
    }
  });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("MCP loopback unavailable");
  return { url: `http://127.0.0.1:${address.port}/mcp`, token,
    async close() {
      lifetime.abort(new Error("Tool session closed"));
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    } };
}
