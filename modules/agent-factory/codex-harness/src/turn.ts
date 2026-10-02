import type { Thread, ThreadEvent, Usage } from "@openai/codex-sdk";
import { metrics, trace, SpanStatusCode } from "@opentelemetry/api";

export interface TurnEvidence {
  response: string;
  usage: Usage;
  threadId: string;
}
export interface TurnContext {
  runId: string;
  personaKey: string;
  model: string;
  harnessRevision: string;
  surface: "github" | "gitlab" | "task-api" | "delegation";
  timeoutMs: number;
  /** @deprecated Ignored; retained for existing host adapters. */
  maxInputBytes?: number;
  maxOutputBytes: number;
  signal: AbortSignal;
  /** Pinned SDK reports cumulative thread usage after a resumed turn. */
  previousUsage?: Usage;
}
export interface Progress {
  type: "turn.started" | "tool.started" | "tool.completed";
  tool?: "command_execution" | "file_change" | "mcp_tool_call" | "web_search";
}

/** Shared SDK turn consumption, independent of persona, provider and invocation.
 * The host supplies an already admitted/isolated SDK thread. Completion here is
 * model completion only; it never acknowledges a task, approves or publishes work.
 */
export async function runSdkTurn(
  thread: Pick<Thread, "runStreamed" | "id">, prompt: string, context: TurnContext,
  progress: (event: Progress) => Promise<void>, outputSchema?: unknown,
): Promise<TurnEvidence> {
  for (const limit of [context.timeoutMs, context.maxOutputBytes]) {
    if (!Number.isSafeInteger(limit) || limit <= 0) throw new Error("Invalid turn limit");
  }
  context.signal.throwIfAborted();
  const local = new AbortController();
  // Detach cancellation after stream shutdown. Aborting the pinned SDK after
  // its iterator exits can emit an unhandled child-process error: the SDK has
  // already removed its listeners at that point.
  const abort = () => local.abort(context.signal.reason);
  context.signal.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => local.abort(new Error("Turn deadline elapsed")), context.timeoutMs);
  const cancel = local.signal;
  const tracer = trace.getTracer("adp.codex-harness");
  const meter = metrics.getMeter("adp.codex-harness");
  // Deliberately bounded metric dimensions: IDs/model strings belong on spans.
  const dimensions = { surface: context.surface, harness: "codex-sdk" };
  const duration = meter.createHistogram("adp.agent.turn.duration", { unit: "s" });
  const tokens = meter.createCounter("adp.agent.tokens", { unit: "{token}" });
  return tracer.startActiveSpan("adp.codex.turn", { attributes: {
    "adp.run.id": context.runId, "adp.persona.key": context.personaKey,
    "adp.harness.revision": context.harnessRevision, "gen_ai.request.model": context.model,
    "adp.invocation.surface": context.surface,
  } }, async span => {
    const started = performance.now();
    let completed = false;
    let succeeded = false;
    let response = "";
    let usage: Usage | undefined;
    try {
      const stream = await thread.runStreamed(prompt, { signal: cancel, outputSchema });
      for await (const event of stream.events) {
        try {
          cancel.throwIfAborted();
          if (completed) throw new Error("SDK emitted events after terminal completion");
          if (event.type === "error" || event.type === "turn.failed") {
            // Provider errors can contain request content or credentials. The host
            // may archive authorized diagnostics separately; telemetry gets a code.
            throw new Error("Codex turn failed; inspect authorized run diagnostics");
          }
          if (event.type === "turn.started") await progress({ type: "turn.started" });
          if (event.type === "item.started" || event.type === "item.completed") {
            const kind = event.item.type;
            if (kind === "command_execution" || kind === "file_change" || kind === "mcp_tool_call" || kind === "web_search") {
              const type = event.type === "item.started" ? "tool.started" : "tool.completed";
              span.addEvent(type, { "adp.tool.kind": kind });
              await progress({ type, tool: kind });
            }
            if (event.type === "item.completed" && kind === "agent_message") {
              response = event.item.text;
              if (Buffer.byteLength(response) > context.maxOutputBytes) throw new Error("Model response exceeds output budget");
            }
          }
          if (event.type === "turn.completed") {
            validateUsage(event);
            usage = event.usage;
            completed = true;
          }
        } catch (error) {
          // Stop a still-active SDK process before iterator cleanup removes its
          // error listeners (output limits and progress failures included).
          if (!cancel.aborted) local.abort(error);
          throw error;
        }
      }
      cancel.throwIfAborted();
      if (!completed || !usage || !thread.id) throw new Error("SDK stream ended without complete turn evidence");
      span.setAttribute("adp.thread.id", thread.id);
      for (const type of ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"] as const) {
        const previous = context.previousUsage?.[type] ?? 0;
        if (usage[type] < previous) throw new Error("SDK cumulative usage regressed");
        tokens.add(usage[type] - previous, { ...dimensions, type });
      }
      succeeded = true;
      span.setStatus({ code: SpanStatusCode.OK });
      return { response, usage, threadId: thread.id };
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR });
      span.setAttribute("error.type", cancel.aborted ? "cancelled_or_deadline" : "turn_failed");
      // Never record arbitrary exception messages, tool text or private reasoning.
      throw error;
    } finally {
      clearTimeout(timer);
      context.signal.removeEventListener("abort", abort);
      duration.record((performance.now() - started) / 1000, { ...dimensions, outcome: succeeded ? "completed" : "failed" });
      span.end();
    }
  });
}

function validateUsage(event: Extract<ThreadEvent, { type: "turn.completed" }>) {
  const fields = ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"] as const;
  if (!event.usage || fields.some(key => !Number.isSafeInteger(event.usage[key]) || event.usage[key] < 0)
      || event.usage.cached_input_tokens > event.usage.input_tokens) {
    throw new Error("SDK returned invalid usage evidence");
  }
}
