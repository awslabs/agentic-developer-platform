import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { TaskTools } from "./task-tools.js";
import { taskToolName } from "./task-adapter.js";
import type { TextResponsesRequest, TextResponsesResult } from "./responses-proxy.js";

const descriptor = () => ({ permission: "validation.run", capability: "tests.run" as const, definition: {
  type: "function" as const, name: taskToolName("validation.run"), description: "Validate bound work.", strict: false as const,
  parameters: { type: "object", additionalProperties: false, properties: { count: { type: "integer", minimum: 1, maximum: 3 } }, required: ["count"] },
} });
const request: TextResponsesRequest = { input: "Inspect", reasoning: { effort: "medium" }, max_output_tokens: 64 };

test("Task frozen JSON schemas validate without coercion and remain unchanged on MCP", () => {
  const source = descriptor();
  const adapter = new TaskTools([source], { responses: async () => { throw new Error(); }, tool: async () => {} }, 4);
  source.definition.parameters.properties.count.minimum = 99;
  const tool = adapter.session().toolBroker.definitions[0]!;
  assert.deepEqual(tool.parameters, descriptor().definition.parameters);
  assert.deepEqual(tool.input.strict().parse({ count: 2 }), { count: 2 });
  for (const args of [{ count: "2" }, {}, { count: 4 }, { count: 2, unknown: true }]) assert.throws(() => tool.input.strict().parse(args));
});

test("Task tool dispatch uses canonical turn and preserves receipts across repair sessions", async () => {
  const tool = descriptor();
  const turnId = randomUUID();
  const call = { type: "function_call" as const, id: "item", namespace: "mcp__adp" as const,
    call_id: "call_1", name: tool.definition.name, arguments: '{"count":2}' };
  let response: TextResponsesResult = { id: "response", status: "completed", output: [call], usage: { input_tokens: 10, output_tokens: 10 } };
  const requests: TextResponsesRequest[] = [];
  let effects = 0;
  const adapter = new TaskTools([tool], {
    responses: async request => { requests.push(request); return { operationStatus: "confirmed", turnId, response }; },
    tool: async (name, args, binding) => {
      assert.equal(name, tool.permission); assert.deepEqual(args, { count: 2 });
      assert.deepEqual(binding, { turn_id: turnId, call_id: "call_1" }); effects++;
      return { operation_status: "confirmed", content: "verified", is_error: false };
    },
  }, 4);
  const signal = new AbortController().signal;
  const session = adapter.session();
  await session.model(request, signal);
  await assert.rejects(session.toolBroker.execute(tool.definition.name, { count: 3 }, signal));
  assert.equal(effects, 0);
  await session.toolBroker.execute(tool.definition.name, { count: 2 }, signal);
  await assert.rejects(session.toolBroker.execute(tool.definition.name, { count: 2 }, signal));
  response = { ...response, output: [{ id: "text", type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text: "report", annotations: [] }] }] };
  await adapter.session().model(request, signal);
  const history = requests[1]!.input;
  assert.ok(Array.isArray(history));
  assert.equal(history[0]!.type, "function_call"); assert.equal(history[1]!.type, "function_call_output");
  assert.equal(JSON.stringify(history).includes("verified"), true);
  assert.equal(effects, 1);
});
