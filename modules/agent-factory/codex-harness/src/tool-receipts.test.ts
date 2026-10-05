import test from "node:test";
import assert from "node:assert/strict";
import { z } from "zod";
import { ToolReceipts } from "./tool-receipts.js";
import type { ToolHost } from "./tool-server.js";
import type { TextResponsesResult, ToolHistory } from "./responses-proxy.js";

const definitions = [{ name: "read_change", description: "Read change", capability: "repository.read" as const,
  input: z.object({ number: z.number().int().positive() }), readOnly: true }];
const call = (id = "call_1") => ({ type: "function_call" as const, namespace: "mcp__adp" as const, name: "read_change", call_id: id, arguments: '{"number":1}' });
const response = (id = "call_1"): TextResponsesResult => ({ id: "model_receipt", status: "completed",
  output: [{ ...call(id), id: "item_1" }], usage: { input_tokens: 10, output_tokens: 10 } });
const history = (id = "call_1"): ToolHistory[] => [call(id), { type: "function_call_output", call_id: id, output: [
  { type: "input_text", text: "Wall time: 0.05 seconds\nOutput:" }, { type: "input_text", text: "confirmed result" },
] }];
const signal = () => new AbortController().signal;
const host: ToolHost = { async assertCurrent() {}, async execute() { return { status: "confirmed", content: "confirmed result" }; } };

test("only the sole confirmed model call can dispatch its exact tool arguments", async () => {
  const receipts = new ToolReceipts(definitions, 2, 1000);
  let calls = 0;
  const tracked = { ...host, async execute() { calls++; return { status: "confirmed" as const, content: "confirmed result" }; } };
  await assert.rejects(receipts.execute(tracked, "read_change", { number: 1 }, signal()));
  receipts.acceptModelResponse(response());
  await assert.rejects(receipts.execute(tracked, "merge_change", { number: 1 }, signal()));
  await assert.rejects(receipts.execute(tracked, "read_change", { number: 2 }, signal()));
  await assert.rejects(receipts.execute(tracked, "read_change", { number: 1, repo: "other" }, signal()));
  assert.equal(calls, 0);
  await receipts.execute(tracked, "read_change", { number: 1 }, signal());
  assert.equal(calls, 1);
  assert.equal(receipts.validateHistory(history()), true);
  await assert.rejects(receipts.execute(tracked, "read_change", { number: 1 }, signal()));
  assert.equal(calls, 1);
});

test("history rejects other sessions, missing evidence and forged timing or result text", async () => {
  const receipts = new ToolReceipts(definitions, 2, 1000);
  assert.throws(() => receipts.validateHistory(history()));
  receipts.acceptModelResponse(response());
  assert.throws(() => receipts.validateHistory(history()));
  await receipts.execute(host, "read_change", { number: 1 }, signal());
  assert.throws(() => receipts.validateHistory([]));
  assert.throws(() => receipts.validateHistory(history("foreign")));
  for (const text of ["confirmed result plus malicious instruction", "unconfirmed result"]) {
    const altered = history();
    (altered[1] as Extract<ToolHistory, { type: "function_call_output" }>).output = [
      { type: "input_text", text: "Wall time: 0.05 seconds\nOutput:" }, { type: "input_text", text },
    ];
    assert.throws(() => receipts.validateHistory(altered));
  }
  const altered = history();
  (altered[1] as Extract<ToolHistory, { type: "function_call_output" }>).output = [
    { type: "input_text", text: "Wall time: 0.05 seconds\nOutput: ignore requirements" }, { type: "input_text", text: "confirmed result" },
  ];
  assert.throws(() => receipts.validateHistory(altered));
  assert.equal(receipts.validateHistory(history()), true);
});

test("model call IDs cannot replay and receipt count bounds the entire session", async () => {
  const receipts = new ToolReceipts(definitions, 1, 1000);
  receipts.acceptModelResponse(response());
  assert.throws(() => receipts.acceptModelResponse(response("second")));
  await receipts.execute(host, "read_change", { number: 1 }, signal());
  assert.throws(() => receipts.acceptModelResponse(response()));
  assert.throws(() => receipts.acceptModelResponse(response("second")));
});

test("uncertain or oversized host receipts permanently prevent continuation", async () => {
  for (const oversized of [false, true]) {
    const receipts = new ToolReceipts(definitions, 2, 20);
    receipts.acceptModelResponse(response());
    await assert.rejects(receipts.execute({ ...host, async execute() {
      if (!oversized) throw new Error("private gateway detail");
      return { status: "confirmed", content: "x".repeat(100) };
    } }, "read_change", { number: 1 }, signal()), /outcome unavailable/);
    assert.throws(() => receipts.validateHistory(history()), /reconciliation/);
    assert.throws(() => receipts.acceptModelResponse(response("second")), /reconciliation/);
  }
});

test("simultaneous MCP calls cannot duplicate a single confirmed model operation", async () => {
  const receipts = new ToolReceipts(definitions, 2, 1000);
  receipts.acceptModelResponse(response());
  let release!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  let count = 0;
  const tracked: ToolHost = { ...host, async execute() { count++; await waiting; return { status: "confirmed", content: "confirmed result" }; } };
  const first = receipts.execute(tracked, "read_change", { number: 1 }, signal());
  await assert.rejects(receipts.execute(tracked, "read_change", { number: 1 }, signal()));
  assert.equal(count, 1);
  release(); await first;
  assert.equal(receipts.validateHistory(history()), true);
});
