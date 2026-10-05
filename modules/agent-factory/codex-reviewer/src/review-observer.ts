import type { ThreadEvent } from '@openai/codex-sdk';
import type { ClosureReport } from './closure-report.js';
import { publishDeveloperEvent, scopedProgress, type DeveloperReporter } from './developer-stream.js';

export interface ReviewObserver extends Pick<DeveloperReporter, 'progress' | 'explanation' | 'activity' | 'session' | 'observeEvent'> {
  control?: {
    signal: AbortSignal;
    socket: string;
    operation<T>(work: () => Promise<T>): Promise<T>;
  };
  finish(result: { summary: string }): Promise<void>;
  fail(error: unknown): Promise<void>;
  closure?(report: ClosureReport): void;
}

export function reviewEvents(observer?: ReviewObserver, structured = false, planScope: 'assignment' | 'inspection' = 'assignment') {
  if (!observer) return undefined;
  const reporter = scopedProgress({ ...observer,
    ...(observer.progress ? { progress: (text: string, detail: Parameters<NonNullable<DeveloperReporter['progress']>>[1]) =>
      observer.progress!(text, { ...detail, ...(detail.category === 'plan' ? { plan_scope: planScope } : {}) }) } : {}),
    async finish() {}, async fail() {},
  });
  return (event: ThreadEvent) => {
    // The structured verdict is published by the controller after validation.
    if (structured && 'item' in event && event.item.type === 'agent_message') return;
    publishDeveloperEvent(event, reporter);
  };
}

export function reviewSignal(signal: AbortSignal, observer?: ReviewObserver): AbortSignal {
  return observer?.control ? AbortSignal.any([signal, observer.control.signal]) : signal;
}

export async function reviewOperation<T>(observer: ReviewObserver | undefined, work: () => Promise<T>): Promise<T> {
  return observer?.control ? observer.control.operation(work) : work();
}
