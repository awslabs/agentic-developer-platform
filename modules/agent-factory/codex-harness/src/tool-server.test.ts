import test from "node:test";
import assert from "node:assert/strict";
import { z } from "zod";
import { startToolServer, type ToolHost, type ToolServerPolicy } from "./tool-server.js";

const tools = [{ name: "read_change", description: "Read the admitted change.", capability: "repository.read" as const,
  input: z.object({ number: z.number().int().positive() }), readOnly: true }];
const policy = (): ToolServerPolicy => ({ capabilities: ["repository.read"], maxCalls: 2,
  maxRequestBytes: 2048, maxResultBytes: 2048, timeoutMs: 1000, signal: new AbortController().signal });
const body = (id: number, args: object = { number: 1 }, name = "read_change") => ({
  jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args },
});
async function post(server: Awaited<ReturnType<typeof startToolServer>>, value: object, token = server.token) {
  const result = await fetch(server.url, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify(value) });
  return { status: result.status, text: await result.text() };
}
const host: ToolHost = { async assertCurrent() {}, async execute() { return { status: "confirmed", content: "admitted change" }; } };

test("host catalogue requires capabilities and rejects duplicate definitions", async () => {
  await assert.rejects(startToolServer(tools, host, { ...policy(), capabilities: [] }), /not admitted/);
  await assert.rejects(startToolServer([...tools, ...tools], host, policy()), /Invalid host tool/);
});

test("bad token, tool and injected arguments never reach privileged execution", async () => {
  let calls = 0;
  const server = await startToolServer(tools, { ...host, async execute() { calls++; return { status: "confirmed", content: "ok" }; } }, policy());
  try {
    assert.equal((await post(server, body(1), "wrong")).status, 401);
    assert.match((await post(server, body(1, { number: 1, repository: "other/repo" }))).text, /refused/);
    assert.match((await post(server, body(2, { number: 1 }, "merge_change"))).text, /refused/);
    assert.equal(calls, 0);
    assert.match((await post(server, body(3))).text, /"text":"ok"/);
    assert.equal(calls, 1);
    assert.match((await post(server, body(3))).text, /refused/);
    assert.equal(calls, 1);
  } finally { await server.close(); }
});

test("revocation before execution or before result disclosure blocks the operation", async () => {
  let allowed = false, calls = 0;
  const server = await startToolServer(tools, {
    async assertCurrent() { if (!allowed) throw new Error("sensitive-policy-reason"); },
    async execute() { calls++; allowed = false; return { status: "confirmed", content: "secret result" }; },
  }, policy());
  try {
    const denied = await post(server, body(1));
    assert.match(denied.text, /refused/); assert.doesNotMatch(denied.text, /sensitive/);
    assert.equal(calls, 0);
    allowed = true;
    const revoked = await post(server, body(2));
    assert.match(revoked.text, /refused/); assert.doesNotMatch(revoked.text, /secret/);
    assert.equal(calls, 1);
    allowed = true;
    assert.equal((await post(server, body(3))).status, 409);
  } finally { await server.close(); }
});

test("unknown outcomes and oversized results poison the session without replay", async () => {
  for (const mode of ["unknown", "oversize"]) {
    let calls = 0;
    const server = await startToolServer(tools, { ...host, async execute() {
      calls++;
      if (mode === "unknown") throw new Error("provider-token-do-not-disclose");
      return { status: "confirmed", content: "x".repeat(4096) };
    } }, policy());
    try {
      const result = await post(server, body(1));
      assert.match(result.text, /refused/); assert.doesNotMatch(result.text, /provider-token/);
      assert.equal((await post(server, body(2))).status, 409);
      assert.equal(calls, 1);
    } finally { await server.close(); }
  }
});

test("confirmed tool errors can continue only within the operation limit", async () => {
  const server = await startToolServer(tools, { ...host, async execute() {
    return { status: "confirmed", content: "Change not found", isError: true };
  } }, { ...policy(), maxCalls: 1 });
  try {
    assert.equal(JSON.parse((await post(server, body(1))).text).result.isError, true);
    assert.match((await post(server, body(2))).text, /refused/);
  } finally { await server.close(); }
});

test("cancellation bounds even an uncooperative host and refuses overlapping calls", async () => {
  const controller = new AbortController();
  let entered!: () => void;
  const entry = new Promise<void>(resolve => { entered = resolve; });
  let active: AbortSignal | undefined;
  const server = await startToolServer(tools, { ...host, async execute(_tool, _args, signal) {
    active = signal; entered(); return new Promise(() => {});
  } }, { ...policy(), signal: controller.signal });
  try {
    const pending = post(server, body(1));
    // Install rejection handling before deliberately closing the client socket.
    const outcome = pending.then(value => value, () => undefined);
    await entry;
    assert.equal((await post(server, body(2))).status, 409);
    controller.abort();
    await outcome;
    assert.equal(active?.aborted, true);
    assert.equal((await post(server, body(3))).status, 409);
  } finally { await server.close(); }
});

test("request bounds and unsupported resource methods do not dispatch; deadline closes unknown work", async () => {
  let calls = 0;
  const server = await startToolServer(tools, { ...host, async execute() {
    calls++; return new Promise(() => {});
  } }, { ...policy(), timeoutMs: 100 });
  try {
    assert.equal((await post(server, body(1, { number: 1, padding: "x".repeat(3000) }))).status, 400);
    assert.match((await post(server, { jsonrpc: "2.0", id: 2, method: "resources/read", params: { uri: "file:///etc/passwd" } })).text, /refused/);
    assert.equal(calls, 0);
    await post(server, body(3)).catch(() => undefined);
    assert.equal(calls, 1);
    assert.equal((await post(server, body(4))).status, 409);
  } finally { await server.close(); }
});


test("host-confirmed SDK exit permits new transport IDs without renewing effect budget", async () => {
  let calls = 0;
  const server = await startToolServer(tools, { ...host, async execute() {
    calls++; return { status: "confirmed", content: "ok" };
  } }, policy());
  try {
    assert.match((await post(server, body(1))).text, /"text":"ok"/);
    assert.match((await post(server, body(1))).text, /refused/);
    server.advanceClient();
    assert.match((await post(server, body(1))).text, /"text":"ok"/);
    server.advanceClient();
    assert.match((await post(server, body(1))).text, /refused/);
    assert.equal(calls, 2);
    assert.throws(() => server.advanceClient(), /cannot advance/);
  } finally { await server.close(); }
});

test("a new SDK client cannot reset an uncertain tool outcome", async () => {
  const server = await startToolServer(tools, { ...host, async execute() { throw new Error("unknown"); } }, policy());
  try {
    assert.match((await post(server, body(1))).text, /refused/);
    assert.throws(() => server.advanceClient(), /cannot advance/);
  } finally { await server.close(); }
});

test('steering may reconnect beyond two repairs without renewing the tool budget', async () => {
  let calls = 0;
  const server = await startToolServer(tools, { ...host, async execute() {
    calls++; return { status: 'confirmed', content: 'ok' };
  } }, { ...policy(), maxClientContinuations: 4 });
  try {
    for (let index = 0; index < 4; index++) server.advanceClient();
    assert.match((await post(server, body(1))).text, /"text":"ok"/);
    assert.match((await post(server, body(2))).text, /"text":"ok"/);
    assert.match((await post(server, body(3))).text, /refused/);
    assert.equal(calls, 2);
    assert.throws(() => server.advanceClient(), /cannot advance/);
  } finally { await server.close(); }
});
