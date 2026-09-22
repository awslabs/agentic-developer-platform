import type { Thread, RunResult, TurnOptions } from "@openai/codex-sdk";
import { setTimeout as pause } from "node:timers/promises";

/** Only interrupted transport is resumable. A refusal, exhausted deadline,
 * invalid verdict or failed test must never be retried as a connection error. */
export function interruptedTransport(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  if (/high-risk cyber|safety|policy|flagged|unauthorized|forbidden|rate limit|quota/i.test(error.message)) return false;
  return /stream disconnected before completion|upstream_stream_(?:timeout|error|incomplete)/i.test(error.message)
    || (error.message.startsWith("Failed to parse item:") && error.cause instanceof SyntaxError);
}

export async function runResumableTurn(
  thread: Pick<Thread, "run" | "id">,
  prompt: string,
  options: TurnOptions,
  wait: (ms: number) => Promise<unknown> = pause,
): Promise<RunResult> {
  for (let attempt = 0; ; attempt++) {
    options.signal?.throwIfAborted();
    try {
      const result = await thread.run(attempt === 0 ? prompt
        : "The transport interrupted the previous turn. Continue that same assignment from the retained conversation and current working tree. Preserve completed work and test evidence; inspect any partially completed command before repeating it. All prior restrictions still apply. Return the required final result.", options);
      // The SDK can return without a terminal event. An arbitrary last message
      // is not a completed review or repair.
      if (!result.usage) throw new Error("stream disconnected before completion: missing turn.completed");
      return result;
    } catch (error) {
      if (attempt >= 1 || !thread.id || options.signal?.aborted || !interruptedTransport(error)) throw error;
      console.error("[codex-reviewer] interrupted transport; resuming the same thread once within the existing deadline");
      await wait(2000);
    }
  }
}
