import type { ThreadEvent } from '@openai/codex-sdk';
import { publishDeveloperEvent, type DeveloperReporter } from './developer-stream.js';

export interface ReviewObserver extends Pick<DeveloperReporter, 'explanation' | 'activity' | 'session' | 'observeEvent'> {
  control?: {
    signal: AbortSignal;
    socket: string;
    operation<T>(work: () => Promise<T>): Promise<T>;
  };
  finish(result: { summary: string }): Promise<void>;
  fail(error: unknown): Promise<void>;
}

export function reviewEvents(observer?: ReviewObserver, structured = false) {
  if (!observer) return undefined;
  return (event: ThreadEvent) => {
    // The structured verdict is published by the controller after validation.
    if (structured && 'item' in event && event.item.type === 'agent_message') return;
    publishDeveloperEvent(event, { ...observer, async finish() {}, async fail() {} });
  };
}

export function reviewSignal(signal: AbortSignal, observer?: ReviewObserver): AbortSignal {
  return observer?.control ? AbortSignal.any([signal, observer.control.signal]) : signal;
}

export async function reviewOperation<T>(observer: ReviewObserver | undefined, work: () => Promise<T>): Promise<T> {
  return observer?.control ? observer.control.operation(work) : work();
}
