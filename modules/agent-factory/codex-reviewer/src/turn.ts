import type { Thread, RunResult, TurnOptions, ThreadEvent } from "@openai/codex-sdk";
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
  thread: Pick<Thread, "run" | "id"> & Partial<Pick<Thread, "runStreamed">>,
  prompt: string,
  options: TurnOptions,
  wait: (ms: number) => Promise<unknown> = pause,
  verifyInstructions: () => void = () => {},
  onEvent?: (event: ThreadEvent) => void | Promise<void>,
): Promise<RunResult> {
  for (let attempt = 0; ; attempt++) {
    options.signal?.throwIfAborted();
    try {
      verifyInstructions();
      const input = attempt === 0 ? prompt
        : "The transport interrupted the previous turn. Continue that same assignment from the retained conversation and current working tree. Preserve completed work and test evidence; inspect any partially completed command before repeating it. All prior restrictions still apply. Return the required final result.";
      let result: RunResult;
      if (onEvent && thread.runStreamed) {
        const { events } = await thread.runStreamed(input, options);
        result = { items: [], finalResponse: '', usage: null };
        for await (const event of events) {
          await onEvent(event);
          if (event.type === 'item.completed') {
            result.items.push(event.item);
            if (event.item.type === 'agent_message') result.finalResponse = event.item.text;
          }
          if (event.type === 'turn.completed') result.usage = event.usage;
          if (event.type === 'turn.failed') throw new Error(event.error.message);
          if (event.type === 'error') throw new Error(event.message);
        }
      } else result = await thread.run(input, options);
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
