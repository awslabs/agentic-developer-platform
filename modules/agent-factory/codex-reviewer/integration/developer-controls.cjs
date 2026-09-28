// Real pinned Codex CLI + local Responses fixture; no credentials or network inference.
// Run after building agent and codex-reviewer: node integration/developer-controls.cjs
const http = require("node:http");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "../../..");
const fs = require("node:fs/promises");
const { CodexControlAdapter } = require(
  path.join(root, "agent-factory/agent/dist/harnesses/codex-control.js"),
);
const { PauseGate } = require(
  path.join(root, "agent-factory/agent/dist/pause-gate.js"),
);
async function verify(mode) {
  const { Codex } = await import("@openai/codex-sdk");
  const home = await fs.mkdtemp("/tmp/adp-controls-sdk-");
  const real = path.resolve(
    __dirname,
    "../node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex",
  );
  await fs.writeFile(
    home + "/codex",
    `#!/bin/sh\nshift\nexec '${real}' exec --dangerously-bypass-hook-trust "$@"\n`,
    { mode: 0o700 },
  );
  let cfg = await fs.readFile(
    path.join(
      root,
      "agent-factory/agent-worker-image/codex-control-config.toml",
    ),
    "utf8",
  );
  cfg = cfg.replaceAll(
    "/app/codex-control-hook.py",
    path.join(root, "agent-factory/agent-worker-image/codex-control-hook.py"),
  );
  await fs.writeFile(home + "/config.toml", cfg);
  const adapter = new CodexControlAdapter(
    new PauseGate({ defaultTimeoutMs: 10000, settleTimeoutMs: 100 }),
  );
  await adapter.start();
  let count = 0;
  const command =
    mode === "abort"
      ? "sleep 5; touch " + home + "/after-abort"
      : "sleep 2; echo CONTROL_PROBE_DONE";
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const b = JSON.parse(raw);
      fs.writeFile(home + "/request" + count + ".json", JSON.stringify(b));
      const tool = b.tools.find((t) =>
        ["shell_command", "exec_command", "shell"].includes(t.name),
      );
      console.log("REQUEST", count, "TOOL", tool?.name);
      const item =
        count++ === 0
          ? {
              type: "function_call",
              id: "fc_1",
              call_id: "call_1",
              name: tool.name,
              arguments: JSON.stringify(
                tool.name === "shell"
                  ? { command: ["bash", "-c", command] }
                  : { command: command, cmd: command, yield_time_ms: 10000 },
              ),
            }
          : {
              type: "message",
              id: "msg_1",
              role: "assistant",
              content: [{ type: "output_text", text: "Done." }],
            };
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      for (const [type, data] of [
        [
          "response.created",
          { response: { id: "resp_" + count, status: "in_progress" } },
        ],
        ["response.output_item.done", { output_index: 0, item }],
        [
          "response.completed",
          {
            response: {
              id: "resp_" + count,
              status: "completed",
              output: [item],
              usage: { input_tokens: 10, output_tokens: 10, total_tokens: 20 },
            },
          },
        ],
      ])
        res.write(
          `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`,
        );
      res.end();
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const client = new Codex({
    codexPathOverride: home + "/codex",
    env: {
      ...process.env,
      CODEX_HOME: home,
      ADP_CODEX_CONTROL_SOCKET: adapter.socket,
    },
    config: {
      model_provider: "probe",
      model_providers: {
        probe: {
          name: "probe",
          base_url: `http://127.0.0.1:${server.address().port}/v1`,
          wire_api: "responses",
        },
      },
    },
  });
  const thread = client.startThread({
    workingDirectory: home,
    skipGitRepoCheck: true,
    model: "gpt-6-sol",
    sandboxMode: "danger-full-access",
    approvalPolicy: "never",
    webSearchMode: "disabled",
  });
  try {
    try {
      const { events } = await thread.runStreamed(
        "Execute the requested command",
        {
          signal: AbortSignal.any([adapter.signal, AbortSignal.timeout(20000)]),
        },
      );
      for await (const e of events) {
        console.log("EVENT", JSON.stringify(e));
        if (e.type === "item.started" && e.item.type === "command_execution") {
          if (mode === "abort") {
            setTimeout(() => adapter.cancel("abort probe"), 200);
            continue;
          }
          console.log("PAUSE", await adapter.requestPause());
          setTimeout(async () => {
            try {
              if (adapter.gate.currentPhase() !== "paused" || count !== 1)
                throw new Error("pause did not hold next model request");
              console.log("PAUSED_CONFIRMED");
              adapter.drainSteering = async () => {
                console.log(
                  "STEER",
                  await adapter.submitInput({
                    kind: "steering",
                    text: "CONTROL_STEERING_PROOF",
                  }),
                );
                adapter.drainSteering = async () => {};
              };
              await adapter.resumeFromPause();
              console.log("RESUMED");
            } catch (e) {
              console.error(e);
              adapter.cancel();
              process.exitCode = 1;
            }
          }, 2500);
        }
      }
    } catch (error) {
      if (mode !== "abort" || !adapter.signal.aborted) throw error;
    }
    if (mode === "abort") {
      assert.equal(adapter.signal.aborted, true);
      await new Promise((r) => setTimeout(r, 5500));
      await assert.rejects(fs.access(home + "/after-abort"), {
        code: "ENOENT",
      });
      console.log("SDK_ABORTED_SHELL_TERMINATED");
      return;
    }
    const request = JSON.parse(
      await fs.readFile(home + "/request1.json", "utf8"),
    );
    if (!JSON.stringify(request).includes("CONTROL_STEERING_PROOF"))
      throw new Error("steering missing from actual model context");
    console.log("STEERING_REACHED_MODEL");
    console.log("CAPABILITIES", adapter.capabilities());
  } finally {
    await adapter.dispose();
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  }
}
verify("pause-steer-resume")
  .then(() => verify("abort"))
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  });
