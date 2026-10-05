import { request } from "node:http";
import { CodexControlAdapter } from "./codex-control";
import { PauseGate } from "../pause-gate";

function hook(
  adapter: CodexControlAdapter,
  event: string,
  id = "tool-1",
  response?: unknown,
  toolName = "Bash",
): Promise<any> {
  return new Promise((resolve, reject) => {
    const req = request(
      { socketPath: adapter.socket, path: "/", method: "POST" },
      (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => resolve(JSON.parse(body)));
      },
    );
    req.on("error", reject);
    req.end(
      JSON.stringify({
        hook_event_name: event,
        tool_use_id: id,
        tool_response: response,
        tool_name: toolName,
      }),
    );
  });
}
let adapter: CodexControlAdapter;
beforeEach(async () => {
  adapter = new CodexControlAdapter(
    new PauseGate({
      defaultTimeoutMs: 20000,
      settleTimeoutMs: 10,
      backgroundWorkProbe: () => adapter.backgroundWorkCount(),
    }),
  );
  await adapter.start();
});
afterEach(async () => {
  await adapter.dispose();
});

test("pause waits for the running shell and holds the next real hook until resume", async () => {
  expect(adapter.capabilities().pause).toBe(false);
  await hook(adapter, "PreToolUse");
  expect(adapter.activeWorkCount()).toBe(1);
  expect((await adapter.requestPause()).outcome).toBe("requested");
  let returned = false;
  const post = hook(adapter, "PostToolUse").then((value) => {
    returned = true;
    return value;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(adapter.gate.currentPhase()).toBe("paused");
  expect(returned).toBe(false);
  await adapter.resumeFromPause();
  await post;
  await hook(adapter, "PreToolUse", "tool-2");
  expect(adapter.activeWorkCount()).toBe(1);
});

test("steering is handed to an observed Codex hook, never during a running tool", async () => {
  await hook(adapter, "PreToolUse");
  expect(
    await adapter.submitInput({ kind: "steering", text: "too early" }),
  ).toBe("rejected");
  adapter.drainSteering = async () => {
    expect(
      await adapter.submitInput({
        kind: "steering",
        text: "Use the corrected requirement",
      }),
    ).toBe("delivered");
  };
  const result = await hook(adapter, "PostToolUse");
  expect(result.hookSpecificOutput.additionalContext).toBe(
    "Use the corrected requirement",
  );
  expect(adapter.canAcceptInput()).toBe(false);
});

test("abort releases parked hooks as denied and cancels SDK execution without a retry", async () => {
  await hook(adapter, "PreToolUse");
  await hook(adapter, "PostToolUse");
  expect((await adapter.requestPause()).outcome).toBe("confirmed");
  const pending = hook(adapter, "PreToolUse", "parked");
  await new Promise((resolve) => setTimeout(resolve, 10));
  adapter.cancel("operator abort");
  expect(adapter.signal.aborted).toBe(true);
  expect((await pending).decision).toBe("block");
  expect(adapter.capabilities()).toEqual({
    pause: false,
    resume: false,
    steer: false,
    abort: false,
  });
});

test("background execution withdraws pause instead of falsely reporting quiescence", async () => {
  await hook(adapter, "PreToolUse");
  await hook(
    adapter,
    "PostToolUse",
    "tool-1",
    "Process running with session ID 123",
  );
  expect(adapter.activeWorkCount()).toBeNull();
  expect(adapter.capabilities().pause).toBe(false);
  expect(adapter.capabilities().abort).toBe(true);
});

 test("delegation cannot falsely confirm a pause after its launching tool returns", async () => {
  await hook(adapter, "PreToolUse", "delegate", undefined, "spawn_agent");
  await hook(adapter, "PostToolUse", "delegate");
  expect(adapter.activeWorkCount()).toBeNull();
  expect((await adapter.requestPause()).outcome).toBe("unavailable");
});

test('SDK command completion settles async Bash with no PostToolUse and holds Stop until resume', async () => {
  await adapter.dispose();
  adapter = new CodexControlAdapter(new PauseGate({ defaultTimeoutMs: 20000, settleTimeoutMs: 10 }), { sdkCommands: true });
  await adapter.start();
  await hook(adapter, 'PreToolUse', 'async-shell');
  adapter.observeSdkEvent({ type: 'item.started', item: { id: 'command-item', type: 'command_execution' } });
  expect((await adapter.requestPause()).outcome).toBe('requested');
  let stopped = false;
  const stop = hook(adapter, 'Stop').then(() => { stopped = true; });
  adapter.observeSdkEvent({ type: 'item.completed', item: { id: 'command-item', type: 'command_execution' } });
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(adapter.activeWorkCount()).toBe(0);
  expect(adapter.gate.currentPhase()).toBe('paused');
  expect(stopped).toBe(false);
  await adapter.resumeFromPause();
  await stop;
});

test('concurrent SDK commands settle once, out of order, without an early paused state', async () => {
  await adapter.dispose();
  adapter = new CodexControlAdapter(new PauseGate({ defaultTimeoutMs: 20000, settleTimeoutMs: 10 }), { sdkCommands: true });
  await adapter.start();
  for (const id of ['one', 'two']) {
    await hook(adapter, 'PreToolUse', id);
    adapter.observeSdkEvent({ type: 'item.started', item: { id, type: 'command_execution' } });
    await hook(adapter, 'PostToolUse', id);
  }
  expect(adapter.activeWorkCount()).toBe(2);
  await adapter.requestPause();
  adapter.observeSdkEvent({ type: 'item.completed', item: { id: 'two', type: 'command_execution' } });
  expect(adapter.activeWorkCount()).toBe(1);
  expect(adapter.gate.currentPhase()).not.toBe('paused');
  adapter.observeSdkEvent({ type: 'item.completed', item: { id: 'one', type: 'command_execution' } });
  await new Promise(resolve => setTimeout(resolve, 20));
  expect(adapter.activeWorkCount()).toBe(0);
  expect(adapter.gate.currentPhase()).toBe('paused');
});
