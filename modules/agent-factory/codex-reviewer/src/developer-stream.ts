import type { Thread, ThreadEvent, RunResult, TurnOptions } from '@openai/codex-sdk';
import { interruptedTransport } from './turn.js';

export interface DeveloperReporter {
  control?: { signal: AbortSignal; socket: string };
  observeEvent?(event: ThreadEvent): void;
  explanation(text: string): void;
  activity(text: string): void;
  session(id: string): void;
  finish(result: { summary: string; prUrl: string; usage?: unknown }): Promise<void>;
  fail(error: unknown): Promise<void>;
}

/** Publish intentional agent messages and tool activity, never reasoning items. */
export function publishDeveloperEvent(event: ThreadEvent, reporter: DeveloperReporter): void {
  reporter.observeEvent?.(event);
  if (event.type === 'thread.started') reporter.session(event.thread_id);
  if (event.type !== 'item.started' && event.type !== 'item.completed') return;
  const item = event.item;
  if (item.type === 'agent_message' && event.type === 'item.completed') reporter.explanation(item.text);
  if (item.type === 'command_execution') reporter.activity(
    `${event.type === 'item.started' ? 'Running' : `Finished (exit ${item.exit_code ?? 'unknown'})`}: ${item.command}`,
  );
  if (item.type === 'file_change' && event.type === 'item.completed') {
    reporter.activity(`Files changed: ${item.changes.map(change => `${change.kind} ${change.path}`).join(', ')}`);
  }
  if (item.type === 'mcp_tool_call') reporter.activity(`${item.server}/${item.tool}: ${item.status}`);
}

export async function runDeveloperStream(
  thread: Pick<Thread, 'runStreamed' | 'id'>, prompt: string, options: TurnOptions, reporter: DeveloperReporter,
  verifyInstructions: () => void = () => {},
): Promise<RunResult> {
  for (let attempt = 0; ; attempt++) {
    try {
      verifyInstructions();
      const { events } = await thread.runStreamed(attempt === 0 ? prompt :
        'The transport interrupted. Continue the same assignment from the current working tree. Preserve completed work and prior instructions; inspect before repeating commands.', options);
      const items: RunResult['items'] = [];
      let finalResponse = '';
      let usage: RunResult['usage'] = null;
      for await (const event of events) {
        publishDeveloperEvent(event, reporter);
        if (event.type === 'item.completed') {
          items.push(event.item);
          if (event.item.type === 'agent_message') finalResponse = event.item.text;
        }
        if (event.type === 'turn.completed') usage = event.usage;
        if (event.type === 'turn.failed') throw new Error(event.error.message);
        if (event.type === 'error') throw new Error(event.message);
      }
      if (!usage) throw new Error('stream disconnected before completion: missing turn.completed');
      return { items, finalResponse, usage };
    } catch (error) {
      if (attempt || !thread.id || options.signal?.aborted || !interruptedTransport(error)) throw error;
      reporter.activity('Connection interrupted; resuming the same SDK session once.');
    }
  }
}
