/** Codex lifecycle hooks feed the same pause gate and signed command journal as Claude. */
import { createServer, type ServerResponse } from "node:http";
import { mkdtemp, chmod, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  CurrentAttemptRegistry,
  newAttemptId,
  CONTROL_PROTOCOL_VERSION,
  type ControlInput,
  type ControlRuntimeListener,
  type InputHandoffResult,
  type PauseResult,
} from "../control-runtime";
import { PauseGate, type AdmissionTicket } from "../pause-gate";

export class CodexControlAdapter {
  private registry = new CurrentAttemptRegistry();
  private tickets = new Map<string, AdmissionTicket>();
  private hooked = false;
  private background = false;
  private boundary:
    | ((input?: ControlInput) => Promise<InputHandoffResult>)
    | null = null;
  private notify?: () => void;
  private server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
      if (body.length > 1024 * 1024) req.destroy();
    });
    req.on("end", () => {
      void Promise.resolve()
        .then(() => this.hook(JSON.parse(body), res))
        .catch(() => {
          this.cancel("Codex control hook failed");
          if (!res.writableEnded)
            res.end(
              JSON.stringify({
                decision: "block",
                reason: "Control hook failed",
              }),
            );
        });
    });
  });
  private directory = "";
  socket = "";
  drainSteering: () => Promise<void> = async () => {};

  constructor(readonly gate: PauseGate) {
    gate.subscribe((event) => {
      const attemptId = this.currentAttempt();
      if (attemptId) this.registry.emit({ ...event, attemptId });
    });
    this.signal.addEventListener("abort", () => gate.cancel("run aborted"), {
      once: true,
    });
  }
  get signal() {
    return this.registry.cancellationSignal;
  }
  describe() {
    return {
      protocolVersion: CONTROL_PROTOCOL_VERSION,
      adapterId: "codex",
      adapterVersion: "0.155.1",
      capabilities: {
        pause: { supported: true },
        resume: { supported: true },
        steer: { supported: true },
        abort: { supported: true },
      },
    };
  }
  capabilities() {
    const live = this.currentAttempt() !== null && !this.isCancelled();
    const safe =
      live &&
      this.hooked &&
      !this.background &&
      !this.gate.barrierBreached() &&
      this.gate.safeBudget() !== null;
    return {
      pause: safe,
      resume: safe,
      steer: live && this.hooked,
      abort: live,
    };
  }
  currentAttempt() {
    return this.registry.currentAttemptId();
  }
  backgroundWorkCount() {
    return this.background ? null : 0;
  }
  activeWorkCount() {
    return this.background ? null : this.gate.activeToolCount();
  }
  isCancelled() {
    return this.registry.isCancelled();
  }
  subscribe(listener: ControlRuntimeListener) {
    return this.registry.subscribe(listener);
  }
  canAcceptInput() {
    return !!this.boundary && !this.isCancelled() && !this.gate.isPauseActive();
  }
  notifyWhenInputAccepted(listener: () => void) {
    this.notify = listener;
  }
  submitInput(input: ControlInput): Promise<InputHandoffResult> {
    return this.registry.deliver(input);
  }
  async requestPause(options?: {
    signal?: AbortSignal;
    timeoutMs?: number;
  }): Promise<PauseResult> {
    if (!this.capabilities().pause || options?.signal?.aborted)
      return {
        outcome: "unavailable",
        reason: "Codex tool boundary is unavailable",
      };
    return this.gate.requestPause({
      timeoutMs: options?.timeoutMs,
      isCurrent: () => !this.isCancelled(),
    });
  }
  async resumeFromPause() {
    await this.gate.resume();
  }
  cancel(reason?: string) {
    this.registry.cancel(reason);
  }
  async start() {
    this.directory = await mkdtemp(join(tmpdir(), "adp-codex-control-"));
    this.socket = join(this.directory, "hooks.sock");
    await new Promise<void>((resolve) =>
      this.server.listen(this.socket, resolve),
    );
    await chmod(this.socket, 0o600);
    await this.registry.attach({
      attemptId: newAttemptId(),
      deliver: (input) =>
        this.canAcceptInput()
          ? this.boundary!(input)
          : Promise.resolve("rejected"),
      canAcceptInput: () => this.canAcceptInput(),
      activeWorkCount: () => this.activeWorkCount(),
      dispose: async () => {},
    });
  }
  async dispose() {
    this.boundary = null;
    await this.registry.dispose();
    this.server.closeAllConnections();
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
    if (this.directory)
      await rm(this.directory, { recursive: true, force: true });
  }
  private async hook(
    input: {
      hook_event_name: string;
      tool_use_id?: string;
      tool_name?: string;
      tool_response?: unknown;
    },
    res: ServerResponse,
  ) {
    res.once("close", () => {
      if (!res.writableFinished) this.cancel("Codex hook transport closed");
    });
    const event = input.hook_event_name;
    if (this.isCancelled()) {
      res.end(JSON.stringify({ decision: "block", reason: "Run aborted" }));
      return;
    }
    this.hooked = true;
    if (event === "PostToolUse") {
      // An asynchronous shell result is not evidence that its process stopped.
      if (
        /Process running with session ID|"session_id"\s*:\s*\d+/.test(
          JSON.stringify(input.tool_response),
        )
      )
        this.background = true;
      this.gate.settle(this.tickets.get(input.tool_use_id ?? ""));
      this.tickets.delete(input.tool_use_id ?? "");
    }
    if (event !== "PreToolUse" && event !== "PostToolUse" && event !== "Stop") {
      res.end("{}");
      return;
    }
    // Park even Stop so a paused execution cannot finalize behind the operator.
    const parked = await this.gate.admit("Codex boundary", this.signal);
    this.gate.settle(parked.ticket);
    if (parked.decision !== "admit") {
      res.end(JSON.stringify({ decision: "block", reason: "Run aborted" }));
      return;
    }
    let replied = false;
    const reply = async (
      instruction?: ControlInput,
    ): Promise<InputHandoffResult> => {
      if (replied) return "rejected";
      replied = true;
      this.boundary = null;
      if (event === "PreToolUse") {
        const admission = await this.gate.admit("Codex tool", this.signal);
        if (admission.decision !== "admit" || !input.tool_use_id) {
          this.gate.settle(admission.ticket);
          res.end(
            JSON.stringify({
              decision: "block",
              reason: "Control admission unavailable",
            }),
          );
          return "rejected";
        }
        if (["Agent", "spawn_agent", "multi_agent_v1"].includes(input.tool_name ?? "")) this.background = true;
        this.tickets.set(input.tool_use_id, admission.ticket!);
      }
      const output = instruction
        ? event === "Stop"
          ? { decision: "block", reason: instruction.text }
          : {
              hookSpecificOutput: {
                hookEventName: event,
                additionalContext: instruction.text,
              },
            }
        : {};
      return new Promise((resolve) => {
        res.once("finish", () => resolve("delivered"));
        res.once("close", () =>
          resolve(res.writableFinished ? "delivered" : "unknown"),
        );
        res.end(JSON.stringify(output));
      });
    };
    // Serialize boundary ownership: concurrent tool hooks may not steal a reader.
    if (!this.boundary && this.gate.activeToolCount() === 0) {
      this.boundary = reply;
      this.notify?.();
      await this.drainSteering();
      if (this.boundary === reply) this.boundary = null;
    }
    if (!replied) await reply();
  }
}
