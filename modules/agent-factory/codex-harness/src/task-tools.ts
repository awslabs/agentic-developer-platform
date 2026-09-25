import assert from "node:assert/strict";
import { Ajv } from "ajv";
import { z } from "zod";
import { taskRuntimeToolSchema } from "./task-adapter.js";
import type { SessionHost } from "./session.js";
import type { HostTool } from "./tool-server.js";
import type { TextResponsesHost, ToolHistory } from "./responses-proxy.js";

type Descriptor = z.infer<typeof taskRuntimeToolSchema>;
type ModelReceipt = Awaited<ReturnType<TextResponsesHost>> & { turnId?: string };
interface TaskBridge {
  responses(request: Parameters<TextResponsesHost>[0]): Promise<ModelReceipt>;
  tool(name: string, args: Record<string, unknown>, modelCall: { turn_id: string; call_id: string }): Promise<unknown>;
}
const confirmedTool = z.object({ operation_status: z.literal("confirmed"), content: z.string().max(32768), is_error: z.boolean() });

/** Compile frozen schemas without coercion, defaults, remote fetches or stripping. */
function compileTool(descriptor: Descriptor): HostTool {
  const parameters = structuredClone(descriptor.definition.parameters);
  if (parameters.type !== "object" || parameters.additionalProperties !== false
    || Buffer.byteLength(JSON.stringify(parameters)) > 16384) throw new Error("Invalid Task tool schema");
  const properties = z.record(z.string(), z.unknown()).parse(parameters.properties ?? {});
  const validate = new Ajv({ strict: true, coerceTypes: false, useDefaults: false, removeAdditional: false }).compile(parameters);
  const input = z.strictObject(Object.fromEntries(Object.keys(properties).map(key => [key, z.unknown().optional()])))
    .superRefine((value, context) => { if (!validate(value)) context.addIssue({ code: "custom", message: "Arguments differ from admitted schema" }); });
  return { name: descriptor.definition.name, description: descriptor.definition.description,
    capability: descriptor.capability, input, parameters, readOnly: false };
}

/** Gateway receipts remain authoritative. Completed history survives SDK report
 * repair sessions; it is never reconstructed from model-authored prose. */
export class TaskTools {
  private readonly descriptors: Descriptor[];
  private readonly definitions: HostTool[];
  private readonly completed: ToolHistory[] = [];
  constructor(descriptors: readonly Descriptor[], private readonly bridge: TaskBridge, private readonly maxCalls: number) {
    this.descriptors = descriptors.map(value => taskRuntimeToolSchema.parse(structuredClone(value)));
    this.definitions = this.descriptors.map(compileTool);
    if (!Number.isSafeInteger(maxCalls) || maxCalls < 1 || maxCalls > 128) throw new Error("Invalid Task tool limit");
  }
  session(): { model: TextResponsesHost; toolBroker: NonNullable<SessionHost["toolBroker"]> } {
    const prior = structuredClone(this.completed);
    let pending: { turn_id: string; call: Extract<ToolHistory, { type: "function_call" }> } | undefined;
    return {
      model: async (request, signal) => {
        signal.throwIfAborted();
        if (pending) throw new Error("Task tool settlement is still outstanding");
        const input = typeof request.input === "string" ? [{ type: "message" as const, role: "user" as const, content: request.input }] : request.input;
        const receipt = await this.bridge.responses({ ...request, input: [...prior, ...input] });
        signal.throwIfAborted();
        const calls = receipt.response.output.filter(item => item.type === "function_call");
        if (calls.length > 1) throw new Error("Parallel Task tools unavailable");
        if (calls.length) {
          const { id: _id, ...call } = calls[0]!;
          if (!receipt.turnId || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(receipt.turnId)) throw new Error("Task model turn receipt missing");
          pending = { turn_id: receipt.turnId, call };
        }
        return receipt;
      },
      toolBroker: {
        definitions: this.definitions, maxCalls: this.maxCalls, repositoryCapabilities: [],
        execute: async (name, args, signal) => {
          signal.throwIfAborted();
          const binding = pending;
          const tool = this.descriptors.find(tool => tool.definition.name === name);
          if (!binding || !tool || binding.call.name !== name || this.completed.length >= this.maxCalls * 2) throw new Error("Task tool has no confirmed model binding");
          assert.deepEqual(JSON.parse(binding.call.arguments), args);
          const result = confirmedTool.parse(await this.bridge.tool(tool.permission, args, { turn_id: binding.turn_id, call_id: binding.call.call_id }));
          signal.throwIfAborted();
          if (Buffer.byteLength(result.content) > 32768) throw new Error("Task tool receipt exceeds bound");
          this.completed.push(binding.call, { type: "function_call_output", call_id: binding.call.call_id, output: [
            { type: "input_text", text: "Wall time: 0 seconds\nOutput:" }, { type: "input_text", text: result.content },
          ] });
          pending = undefined;
          return { status: "confirmed", content: result.content, isError: result.is_error };
        },
      },
    };
  }
}
