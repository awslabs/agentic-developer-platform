import { query } from '@anthropic-ai/claude-agent-sdk';
import { startControlRuntime, type ControlRuntimeStartResult } from './control-runtime-factory';
import { resilientQuery } from './utils/resilientQuery';
import { ClaudeProgress } from './claude-progress';
import { restoreOperationsStoryBoard } from './operations-story-board';

/** Attach the registered operations worker to the same controls as ordinary runs. */
export async function* operationsQuery(params: Parameters<typeof query>[0], restored?: (board: Awaited<ReturnType<typeof restoreOperationsStoryBoard>>) => void) {
  let control: ControlRuntimeStartResult | undefined;
  try {
    try {
      control = await startControlRuntime({
        log: (level, message) => console.error(`Operations control ${level}: ${message}`),
      });
      if (!control.outcome.started && control.outcome.reason !== 'disabled') {
        console.error(`Operations control unavailable: ${control.outcome.reason}`);
      }
    } catch {
      // A control startup failure must not discard an admitted operations task.
      console.error('Operations control setup failed; continuing without controls');
    }
    const runtime = control?.runtime;
    const board = await restoreOperationsStoryBoard((text, detail) => control?.events?.publish(text, detail));
    restored?.(board);
    if (typeof params.prompt !== 'string') throw new Error('Operations story requires a text prompt');
    params = { ...params, prompt: `${params.prompt}\n\n${board.context}` };
    const progress = control?.events ? new ClaudeProgress(control.events, board.checklist) : undefined;
    const messages = runtime ? resilientQuery({
      queryParams: params,
      // Transport wiring must not add retries or reset the story's turn budget.
      maxRetries: 0,
      // The entry point publishes the bounded SDK diagnostic and report.
      log: () => {},
      attemptInputFactory: runtime.adapter.attemptInputFactory(pause => ({ hooks: {
        PreToolUse: [{ timeout: pause.preToolUseTimeoutSeconds, hooks: [pause.preToolUse] }],
        PostToolUse: [{ hooks: [pause.postToolUse] }],
        PostToolUseFailure: [{ hooks: [pause.postToolUse] }],
        Stop: [{ hooks: [pause.onStop] }],
        SubagentStop: [{ hooks: [pause.onStop] }],
      } })),
      onAttemptHandle: runtime.adapter.onAttemptHandle(),
      cancellation: runtime.adapter.cancellationSource(),
      beforeOutput: () => runtime.gate.waitForOutput(),
      idleSuspended: () => runtime.gate.isPauseActive(),
    }) : query(params);
    for await (const message of messages) {
      if (progress) progress.observe(message);
      else if (message.type === 'assistant' && Array.isArray(message.message?.content)) {
        // Private persistence is independent of public explanation/control startup.
        for (const block of message.message.content) board.checklist(block);
      }
      yield message;
    }
  } finally {
    control?.events?.finish();
    control?.runtime?.steerQueue.dispose();
    try {
      await control?.listener?.stop();
    } catch {
      console.error('Operations control listener cleanup failed');
    }
    try {
      await control?.runtime?.adapter.dispose();
    } catch {
      console.error('Operations control adapter cleanup failed');
    }
  }
}
