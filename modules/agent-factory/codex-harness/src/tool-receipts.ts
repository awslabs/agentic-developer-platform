import assert from "node:assert/strict";
import { z } from "zod";
import type { HostTool, ToolHost } from "./tool-server.js";
import type { TextResponsesResult, ToolHistory } from "./responses-proxy.js";

const callSchema = z.strictObject({ type: z.literal("function_call"), id: z.string().min(1).max(200).optional(),
  call_id: z.string().min(1).max(200), namespace: z.literal("mcp__adp"),
  name: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/), arguments: z.string().min(2).max(32768),
  status: z.literal("completed").optional() });
type Call = z.infer<typeof callSchema>;
type Receipt = Awaited<ReturnType<ToolHost["execute"]>>;
interface Entry { call: Call; args: Record<string, unknown>; receipt?: Receipt }

/** Per-session binding between confirmed model calls, host execution and SDK
 * history. This is not a durable operation journal or resume authority. The
 * wrapped host still authenticates, reauthorizes and persists every operation.
 * A new session starts empty and cannot accept another session's history. */
export class ToolReceipts {
  private readonly tools: Map<string, HostTool>;
  private readonly entries = new Map<string, Entry>();
  private pending?: Entry;
  private executing = false;
  private failed = false;
  constructor(tools: readonly HostTool[], private readonly maxCalls: number | undefined, private readonly maxReceiptBytes: number) {
    if ((maxCalls !== undefined && (!Number.isSafeInteger(maxCalls) || maxCalls < 1 || maxCalls > 128))
      || !Number.isSafeInteger(maxReceiptBytes) || maxReceiptBytes < 1 || maxReceiptBytes > 32768
      || tools.length > 64 || new Set(tools.map(tool => tool.name)).size !== tools.length) throw new Error("Invalid tool receipt policy");
    this.tools = new Map(tools.map(tool => [tool.name, { ...tool, input: tool.input.strict() }]));
  }
  private current() { if (this.failed) throw new Error("Tool session requires reconciliation"); }

  /** Called only for a confirmed model response, before it reaches the SDK. */
  acceptModelResponse(response: TextResponsesResult): void {
    this.current();
    if (this.pending || this.executing) throw new Error("Tool result still outstanding");
    const calls = response.output.filter(item => item.type === "function_call");
    if (calls.length > 1) throw new Error("Parallel tools not supported");
    if (!calls.length) return;
    const call = callSchema.parse(calls[0]);
    const tool = this.tools.get(call.name);
    if (!tool || this.entries.has(call.call_id) || (this.maxCalls !== undefined && this.entries.size >= this.maxCalls)) throw new Error("Tool call not admitted");
    const args = tool.input.parse(JSON.parse(call.arguments));
    const entry = { call, args };
    this.entries.set(call.call_id, entry);
    this.pending = entry;
  }

  /** MCP lacks the Responses call ID. Serial execution binds the exact tool and
   * parsed arguments to the sole outstanding confirmed model call. */
  async execute(host: ToolHost, name: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Receipt> {
    this.current();
    signal.throwIfAborted();
    const entry = this.pending;
    if (!entry || this.executing || entry.call.name !== name) throw new Error("No matching confirmed model call");
    try { assert.deepEqual(args, entry.args); }
    catch { throw new Error("Tool arguments differ from confirmed model call"); }
    this.executing = true;
    try {
      const receipt = z.strictObject({ status: z.literal("confirmed"), content: z.string(), isError: z.boolean().optional() })
        .parse(await host.execute(name, structuredClone(args), signal));
      signal.throwIfAborted();
      if (Buffer.byteLength(receipt.content) > this.maxReceiptBytes) throw new Error("Tool receipt exceeds bound");
      // MCP transport rechecks authority before disclosing this receipt. Failure
      // there poisons that transport, so it cannot resume model execution.
      entry.receipt = Object.freeze({ ...receipt });
      this.pending = undefined;
      return { ...receipt };
    } catch {
      this.failed = true;
      throw new Error("Tool outcome unavailable; reconcile through host");
    } finally { this.executing = false; }
  }

  /** Verify all prior call/result pairs in order, not only IDs or substrings.
   * The SDK's wall-time prefix is transport decoration, not evidence. */
  validateHistory(history: readonly ToolHistory[]): true {
    this.current();
    if (this.pending || this.executing || history.length !== this.entries.size * 2) throw new Error("Incomplete tool receipt history");
    let index = 0;
    for (const entry of this.entries.values()) {
      const call = history[index++], output = history[index++];
      if (!call || !output || call.type !== "function_call" || output.type !== "function_call_output" || !entry.receipt
        || call.call_id !== entry.call.call_id || call.name !== entry.call.name || call.namespace !== "mcp__adp"
        || output.call_id !== call.call_id) throw new Error("Tool receipt identity mismatch");
      try { assert.deepEqual(JSON.parse(call.arguments), entry.args); }
      catch { throw new Error("Tool history arguments mismatch"); }
      // Host tools return one text part. The pinned SDK renders that part after
      // its timing prefix. No additional content, images or URIs are accepted.
      const parts = output.output;
      if (!Array.isArray(parts) || parts.length !== 2 || parts[0]?.type !== "input_text" || parts[1]?.type !== "input_text"
        || !/^Wall time: [0-9]{1,8}(?:\.[0-9]{1,9})? seconds\nOutput:$/.test(parts[0].text)
        || parts[1].text !== entry.receipt.content) throw new Error("Tool history output differs from confirmed receipt");
    }
    return true;
  }
}
