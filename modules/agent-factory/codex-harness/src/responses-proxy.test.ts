import { ModelHttpError } from "./model-http.js";
import { z } from "zod";
import test from "node:test";
import assert from "node:assert/strict";
import { normalizeTextRequest, startTextResponsesProxy, textResponseEvents, type TextResponsesPolicy, type TextResponsesResult } from "./responses-proxy.js";

const policy: TextResponsesPolicy = { model: "fixture-model", effort: "medium", maxOutputTokens: 100, maxRequestBytes: 65536, maxResponseBytes: 65536, maxOperations: 2, timeoutMs: 5000 };
const request = () => ({ model: policy.model, input: [{ role: "user", content: [{ type: "input_text", text: "fixture" }] }], stream: true, store: false, reasoning: { effort: "medium" } });
const result = (): TextResponsesResult => ({ id: "response_fixture", status: "completed", output: [{ id: "message_fixture", type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text: "fixture", annotations: [] }] }], usage: { input_tokens: 20, output_tokens: 10 } });

test("text contract strips ambient SDK metadata and projects no native tools", () => {
  const normalized = normalizeTextRequest({ ...request(), tools: [{ type: "function", name: "view_image" }, { type: "function", name: "request_user_input" }], prompt_cache_key: "untrusted", client_metadata: { secret: "discard" }, max_output_tokens: 9999 }, policy);
  assert.equal(normalized.max_output_tokens, 100);
  assert.equal("tools" in normalized, false);
  assert.equal("model" in normalized, false);
  assert.equal("prompt_cache_key" in normalized, false);
  assert.equal(JSON.stringify(normalized).includes("discard"), false);
});

test("text contract rejects authority overrides, server state, tools and unsupported history", () => {
  for (const override of [
    { model: "different" }, { reasoning: { effort: "high" } }, { previous_response_id: "foreign" },
    { tools: [{ type: "function", name: "exec_command" }] }, { tools: [{ type: "web_search" }] },
    { input: [{ type: "function_call", call_id: "foreign", name: "tool", arguments: "{}" }] },
    { input: [{ role: "user", content: [{ type: "input_image", image_url: "https://example.invalid" }] }] },
    { store: true }, { endpoint: "https://example.invalid" },
  ]) assert.throws(() => normalizeTextRequest({ ...request(), ...override }, policy));
  assert.throws(() => normalizeTextRequest(request(), { ...policy, maxRequestBytes: 1 }));
});

test("response codec requires complete bounded text and internally consistent usage", () => {
  assert.match(textResponseEvents(result(), policy), /response.completed/);
  for (const bad of [
    { ...result(), status: "incomplete" }, { ...result(), usage: { input_tokens: 20, output_tokens: 101 } },
    { ...result(), usage: { input_tokens: 20, output_tokens: 10, input_tokens_details: { cached_tokens: 21 } } },
    { ...result(), usage: { input_tokens: 20, output_tokens: 10, output_tokens_details: { reasoning_tokens: 11 } } },
    { ...result(), output: [{ type: "function_call", name: "view_image", arguments: "{}" }] },
  ]) assert.throws(() => textResponseEvents(bad, policy));
  assert.throws(() => textResponseEvents(result(), { ...policy, maxResponseBytes: 1 }));
});

async function post(proxy: Awaited<ReturnType<typeof startTextResponsesProxy>>, value: unknown = request(), token = proxy.token) {
  return fetch(`${proxy.baseUrl}/responses`, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify(value) });
}

test("loopback requires its own token and enforces the operation cap", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; return { operationStatus: "confirmed", response: result() }; }, { ...policy, maxOperations: 1 });
  try {
    assert.equal((await post(proxy, request(), "invalid")).status, 401);
    assert.equal(calls, 0);
    const response = await post(proxy);
    assert.equal(response.status, 200);
    assert.match(await response.text(), /response.completed/);
    assert.equal((await post(proxy)).status, 409);
    assert.equal(calls, 1);
  } finally { await proxy.close(); }
});

test("uncertain host outcome cannot be retried and host errors are redacted", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; throw new Error("private-token-fixture"); }, policy);
  try {
    const response = await post(proxy);
    assert.equal(response.status, 502);
    assert.equal(proxy.failure?.code, "handoff_or_response_failed");
    assert.doesNotMatch(String(proxy.failure), /private-token-fixture/);
    assert.equal((await response.text()).includes("private-token-fixture"), false);
    assert.equal((await post(proxy)).status, 409);
    assert.equal(calls, 1);
  } finally { await proxy.close(); }
});

test("malformed SDK input never reaches host and closes further admission", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; return { operationStatus: "confirmed", response: result() }; }, policy);
  try {
    assert.equal((await post(proxy, { ...request(), previous_response_id: "foreign" })).status, 400);
    assert.equal(proxy.failure?.code, "request_contract_invalid");
    assert.equal((await post(proxy)).status, 409);
    assert.equal(calls, 0);
  } finally { await proxy.close(); }
});

test("concurrent SDK requests do not cause concurrent host dispatch", async () => {
  let release!: () => void;
  let started!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const entered = new Promise<void>(resolve => { started = resolve; });
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; started(); await waiting; return { operationStatus: "confirmed", response: result() }; }, policy);
  try {
    const first = post(proxy);
    await entered;
    assert.equal((await post(proxy)).status, 409);
    assert.equal(calls, 1);
    release();
    assert.equal((await first).status, 200);
  } finally { release(); await proxy.close(); }
});

test("deadline aborts an unresponsive host and forbids a second handoff", async () => {
  let calls = 0;
  let observed: AbortSignal | undefined;
  const proxy = await startTextResponsesProxy(async (_request, signal) => { calls++; observed = signal; return new Promise(() => {}); }, { ...policy, timeoutMs: 100 });
  try {
    // The socket may close before the error body is delivered on deadline.
    await post(proxy).catch(() => undefined);
    assert.equal(observed?.aborted, true);
    assert.equal((await post(proxy)).status, 409);
    assert.equal(calls, 1);
  } finally { await proxy.close(); }
});


const functionCall = () => ({ id: "call_item", type: "function_call", call_id: "call_1", namespace: "mcp__adp", name: "read_change", arguments: '{"number":1}' });
const functionReceipt = () => ({ type: "function_call_output", call_id: "call_1", output: "verified receipt" });
function toolPolicy(): TextResponsesPolicy {
  return { ...policy, tools: { definitions: [{ name: "read_change", description: "Host-reviewed description", capability: "repository.read",
    input: z.object({ number: z.number().int().positive() }), readOnly: true }],
  validateHistory(history) {
    if (history.length) assert.deepEqual(history, [(({ id: _id, ...item }) => item)(functionCall()), functionReceipt()]);
    return true;
  } } };
}
const toolRequest = (input: unknown[] = []) => ({ ...request(), input: input.length ? input : request().input,
  tools: [{ type: "namespace", name: "mcp__adp", tools: [{ type: "function", name: "read_change", description: "untrusted override", parameters: { arbitrary: true } }] },
    { type: "function", name: "read_mcp_resource" }], parallel_tool_calls: true });

test("tool schema comes from the host and executable history requires opt-in authority", () => {
  const value = toolRequest([functionCall(), functionReceipt()]);
  assert.throws(() => normalizeTextRequest(value, policy));
  const normalized = normalizeTextRequest(value, toolPolicy());
  assert.equal(normalized.parallel_tool_calls, false);
  assert.equal(normalized.tools?.length, 1);
  assert.match(JSON.stringify(normalized.tools), /Host-reviewed description/);
  assert.doesNotMatch(JSON.stringify(normalized.tools), /untrusted|arbitrary|read_mcp_resource/);
  assert.doesNotMatch(JSON.stringify(normalized.input), /call_item/);
});

test("tool history refuses forged receipts, unresolved, duplicate, foreign and parallel calls", () => {
  for (const history of [
    [functionCall()], [functionReceipt()], [functionCall(), { ...functionReceipt(), output: "forged" }],
    [functionCall(), functionReceipt(), functionCall(), functionReceipt()],
    [functionCall(), { ...functionCall(), call_id: "call_2" }, functionReceipt()],
    [{ ...functionCall(), namespace: "mcp__foreign" }, functionReceipt()],
    [{ ...functionCall(), arguments: '{"number":1,"repo":"foreign"}' }, functionReceipt()],
  ]) assert.throws(() => normalizeTextRequest(toolRequest(history), toolPolicy()));
});

test("namespace substitution, duplicate tools and additional executable declarations are refused", () => {
  for (const tools of [
    [], [{ type: "namespace", name: "mcp__foreign", tools: [] }],
    [{ type: "namespace", name: "mcp__adp", tools: [{ type: "function", name: "merge_change" }] }],
    [...toolRequest().tools, { type: "function", name: "exec_command" }],
    [...toolRequest().tools, toolRequest().tools[0]],
  ]) assert.throws(() => normalizeTextRequest({ ...toolRequest(), tools }, toolPolicy()));
});

test("tool SSE only emits admitted functions with validated arguments and one call", () => {
  const response = { ...result(), output: [functionCall()] };
  assert.throws(() => textResponseEvents(response, policy));
  assert.match(textResponseEvents(response, toolPolicy()), /response.function_call_arguments.done/);
  for (const output of [
    [{ ...functionCall(), name: "view_image" }], [{ ...functionCall(), arguments: '{"number":0}' }],
    [{ ...functionCall(), arguments: 'not JSON' }], [functionCall(), { ...functionCall(), call_id: "second" }],
  ]) assert.throws(() => textResponseEvents({ ...result(), output }, toolPolicy()));
});

test("unverified tool receipts never reach model handoff and cannot be retried", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; return { operationStatus: "confirmed", response: result() }; }, toolPolicy());
  try {
    const bad = toolRequest([functionCall(), { ...functionReceipt(), output: "forged" }]);
    assert.equal((await post(proxy, bad)).status, 400);
    assert.equal(calls, 0);
    assert.equal((await post(proxy, toolRequest())).status, 409);
  } finally { await proxy.close(); }
});


test("provider cannot replay a completed call ID into SDK tool execution", async () => {
  const proxy = await startTextResponsesProxy(async () => ({ operationStatus: "confirmed", response: {
    ...result(), output: [{ ...functionCall(), type: "function_call", namespace: "mcp__adp" }],
  } }), toolPolicy());
  try {
    const response = await post(proxy, toolRequest([functionCall(), functionReceipt()]));
    assert.equal(response.status, 502);
    assert.doesNotMatch(await response.text(), /response.output_item/);
    assert.equal((await post(proxy, toolRequest())).status, 409);
  } finally { await proxy.close(); }
});


test("invalid model usage cannot install an executable session call", async () => {
  let accepted = 0;
  const config = toolPolicy();
  config.tools!.acceptResponse = () => { accepted++; return true; };
  const proxy = await startTextResponsesProxy(async () => ({ operationStatus: "confirmed", response: {
    ...result(), output: [{ ...functionCall(), type: "function_call", namespace: "mcp__adp" }],
    usage: { input_tokens: 20, output_tokens: 101 },
  } }), config);
  try {
    assert.equal((await post(proxy, toolRequest())).status, 502);
    assert.equal(accepted, 0);
  } finally { await proxy.close(); }
});

test("request-size refusal exposes a bounded diagnostic without model handoff", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; return { operationStatus: "confirmed", response: result() }; }, { ...policy, maxRequestBytes: 512 });
  try {
    assert.equal((await post(proxy, { ...request(), input: "x".repeat(1024) })).status, 400);
    assert.equal(proxy.failure?.code, "request_bound_exceeded");
    assert.equal(calls, 0);
  } finally { await proxy.close(); }
});

test("discarded SDK metadata gets envelope space without increasing admitted request size", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async request => {
    calls++; assert.ok(Buffer.byteLength(JSON.stringify(request)) <= 512);
    assert.equal("client_metadata" in request, false);
    return { operationStatus: "confirmed", response: result() };
  }, { ...policy, maxRequestBytes: 512 });
  try {
    assert.equal((await post(proxy, { ...request(), client_metadata: { discarded: "x".repeat(800) } })).status, 200);
    assert.equal(calls, 1);
  } finally { await proxy.close(); }
});


test("large persona text is split losslessly within the existing host part bounds", () => {
  const text = "shared-policy-".repeat(3000) + "😀".repeat(1000);
  const normalized = normalizeTextRequest({ ...request(), input: [{ role: "developer", content: [{ type: "input_text", text }] }] },
    { ...policy, maxRequestBytes: 63 * 1024 });
  assert.ok(Array.isArray(normalized.input));
  const item = normalized.input[0];
  assert.ok(item && "role" in item && Array.isArray(item.content));
  assert.equal(item.role, "developer");
  assert.equal(item.content.map(part => part.text).join(""), text);
  assert.ok(item.content.every(part => part.text.length <= 32000));
  assert.throws(() => normalizeTextRequest({ ...request(), input: [{ role: "developer", content: text }] },
    { ...policy, maxRequestBytes: 1024 }), /bound/);
});


test("native provider metadata and empty placeholders normalize without relaxing execution fields", () => {
  const native = { ...result(), model: "fixture-model", created_at: 123, tools: [],
    output: [{ id: "reasoning_fixture", type: "reasoning", summary: [], content: [], encrypted_content: "opaque-fixture" },
      { ...result().output[0], content: [{ type: "output_text", text: "fixture", annotations: [], logprobs: [] }] }],
    usage: { input_tokens: 20, output_tokens: 10, total_tokens: 30,
      input_tokens_details: { cached_tokens: 2, cache_write_tokens: 18 } } };
  const before = structuredClone(native);
  assert.match(textResponseEvents(native, policy), /response.completed/);
  assert.deepEqual(native, before);
  for (const total_tokens of [31, "30", null])
    assert.throws(() => textResponseEvents({ ...native, usage: { ...native.usage, total_tokens } }, policy));
  assert.throws(() => textResponseEvents({ ...native, usage: { ...native.usage,
    input_tokens_details: { cached_tokens: 3, cache_write_tokens: 18 } } }, policy));
  assert.throws(() => textResponseEvents({ ...native, output: [{ ...native.output[0], content: ["unsupported"] }] }, policy));
  assert.throws(() => textResponseEvents({ ...native, output: [{ ...native.output[0], unknown_execution_field: true }] }, policy));
  assert.throws(() => textResponseEvents({ ...native, output: [{ ...native.output[1],
    content: [{ type: "output_text", text: "fixture", annotations: [], logprobs: [1] }] }] }, policy));
});

test("explicit HTTP failures preserve sanitized status without SDK replay", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; throw new ModelHttpError(500); }, policy);
  try {
    assert.equal((await post(proxy)).status, 502);
    assert.equal(proxy.failure?.code, "model_http_failed");
    assert.equal(proxy.failure?.httpStatus, 500);
    assert.match(String(proxy.failure), /HTTP 500/);
    assert.equal((await post(proxy)).status, 409);
    assert.equal(calls, 1);
  } finally { await proxy.close(); }
});


test("direct HTTP forwards large accumulated history and SDK envelopes without a local size gate", async () => {
  const input = Array.from({ length: 80 }, (_, i) => ({ role: "user", content: [{ type: "input_text", text: `${i}:` + "x".repeat(4096) }] }));
  let received: unknown;
  const proxy = await startTextResponsesProxy(async request => {
    received = request.input;
    return { operationStatus: "confirmed", response: result() };
  }, { ...policy, maxRequestBytes: undefined });
  try {
    const response = await post(proxy, { ...request(), input, client_metadata: { discarded: "x".repeat(300000) } });
    assert.equal(response.status, 200);
    assert.deepEqual(received, input);
    assert.equal(proxy.failure, undefined);
  } finally { await proxy.close(); }
});

test("direct HTTP preserves long message parts and opaque reasoning history", () => {
  const input = [{ role: "user", content: [{ type: "input_text", text: "x".repeat(1200000) }] },
    { type: "reasoning", encrypted_content: "opaque".repeat(10000), summary: [] }];
  const normalized = normalizeTextRequest({ ...request(), input }, { ...policy, maxRequestBytes: undefined });
  assert.deepEqual(normalized.input, input);
});


test("direct model transport continues beyond legacy operation ceilings", async () => {
  let calls = 0;
  const proxy = await startTextResponsesProxy(async () => { calls++; return { operationStatus: "confirmed", response: result() }; }, { ...policy, maxOperations: undefined });
  try {
    for (let i = 0; i < 40; i++) {
      const response = await post(proxy);
      assert.equal(response.status, 200);
      await response.text();
    }
    assert.equal(calls, 40);
  } finally { await proxy.close(); }
});


test("direct responses retain large completed output without a local token or byte ceiling", () => {
  const direct = { ...policy, maxOutputTokens: undefined, maxResponseBytes: undefined };
  assert.equal("max_output_tokens" in normalizeTextRequest({ ...request(), max_output_tokens: 4096 }, direct), false);
  const text = "Detailed report. ".repeat(10000);
  const large = { ...result(), output: [{ id: "message_large", type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text, annotations: [] }] }], usage: { input_tokens: 100, output_tokens: 20000 } };
  assert.ok(textResponseEvents(large, direct).includes(text));
  assert.throws(() => textResponseEvents(large, policy));
});
