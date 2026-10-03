import { randomUUID } from 'node:crypto';
import type { Thread, ThreadEvent, RunResult, TurnOptions } from '@openai/codex-sdk';
import { interruptedTransport } from './turn.js';

export interface DeveloperReporter {
  control?: { signal: AbortSignal; socket: string };
  observeEvent?(event: ThreadEvent): void;
  progress?(text: string, detail: { id: string; category: 'message' | 'tool' | 'plan'; state: 'running' | 'completed' | 'failed' }): void;
  explanation(text: string): void;
  activity(text: string): void;
  session(id: string): void;
  finish(result: { summary: string; prUrl: string; usage?: unknown }): Promise<void>;
  fail(error: unknown): Promise<void>;
}

/** SDK item IDs may restart at zero on each CLI turn. Keep UI identities unique. */
export function scopedProgress<T extends Pick<DeveloperReporter, 'progress'>>(reporter: T): T {
  const scope = randomUUID();
  return { ...reporter, ...(reporter.progress ? { progress: (text: string, detail: Parameters<NonNullable<DeveloperReporter['progress']>>[1]) =>
    reporter.progress!(text, { ...detail, id: `${scope}:${detail.id}` }) } : {}) };
}

/** Publish intentional agent messages and tool activity, never reasoning items. */
export function publishDeveloperEvent(event: ThreadEvent, reporter: DeveloperReporter): void {
  reporter.observeEvent?.(event);
  if (event.type === 'thread.started') reporter.session(event.thread_id);
  if (event.type !== 'item.started' && event.type !== 'item.updated' && event.type !== 'item.completed') return;
  const item = event.item;
  const completed = event.type === 'item.completed';
  const emit = (text: string, category: 'message' | 'tool' | 'plan', failed = false) => {
    if (reporter.progress) reporter.progress(text, { id: item.id, category, state: failed ? 'failed' : completed ? 'completed' : 'running' });
    else if (category === 'message') reporter.explanation(text);
    else reporter.activity(text);
  };
  if (item.type === 'agent_message' && item.text.trim()) emit(item.text, 'message');
  if (item.type === 'command_execution' && event.type !== 'item.updated') emit(
    `${completed ? `Finished (exit ${item.exit_code ?? 'unknown'})` : 'Running'}: ${item.command}`, 'tool', item.status === 'failed',
  );
  if (item.type === 'file_change' && completed) emit(
    `Files changed: ${item.changes.map(change => `${change.kind} ${change.path}`).join(', ')}`, 'tool', item.status === 'failed');
  if (item.type === 'mcp_tool_call' && event.type !== 'item.updated') emit(`${item.server}/${item.tool}: ${item.status}`, 'tool', item.status === 'failed');
  if (item.type === 'web_search' && event.type !== 'item.updated') emit(`${completed ? 'Searched' : 'Searching'} the web: ${item.query}`, 'tool');
  if (item.type === 'todo_list') emit(item.items.map(step => `${step.completed ? '✓' : '○'} ${step.text}`).join('\n'), 'plan');
  if (item.type === 'error' && completed) emit(`Agent reported: ${item.message}`, 'message', true);
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
      const streamReporter = scopedProgress(reporter);
      for await (const event of events) {
        publishDeveloperEvent(event, streamReporter);
        if (event.type === 'item.completed') {
          items.push(event.item);
          if (event.item.type === 'agent_message') finalResponse = event.item.text;
        }
        if (event.type === 'turn.completed') usage = event.usage;
        if (event.type === 'turn.failed') throw new Error(event.error.message);
        // SDK `error` events include native reconnect notifications. Like
        // Thread.run(), consume them; only turn.failed is terminal.
      }
      if (!usage) throw new Error('stream disconnected before completion: missing turn.completed');
      return { items, finalResponse, usage };
    } catch (error) {
      if (attempt || !thread.id || options.signal?.aborted || !interruptedTransport(error)) throw error;
      reporter.activity('Connection interrupted; resuming the same SDK session once.');
    }
  }
}
