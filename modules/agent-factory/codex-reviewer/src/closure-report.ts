import type { Task } from './task-board.js';

export interface ReviewClosure {
  completed: string[];
  remaining: string[];
  verifiedTasks: { id: string; evidence: string }[];
}

export interface ClosureReport {
  summary: string;
  completed: string[];
  remaining: string[];
  delivery: string;
  reviewed_revision?: string;
  reporting_notes: string[];
}

export const reviewClosureSchema = {
  type: 'object', additionalProperties: false,
  properties: {
    completed: { type: 'array', items: { type: 'string' } },
    remaining: { type: 'array', items: { type: 'string' } },
    verifiedTasks: { type: 'array', items: {
      type: 'object', additionalProperties: false,
      properties: { id: { type: 'string' }, evidence: { type: 'string' } },
      required: ['id', 'evidence'],
    } },
  },
  required: ['completed', 'remaining', 'verifiedTasks'],
};

/** Reporting is optional. Malformed metadata must not invalidate a sound review. */
export function parseReviewClosure(value: unknown): ReviewClosure | undefined {
  if (!value || typeof value !== 'object') return;
  const v = value as Record<string, unknown>;
  const lines = (a: unknown): a is string[] => Array.isArray(a) && a.length <= 100
    && a.every(s => typeof s === 'string' && s.trim().length > 0 && s.length <= 4096);
  if (!lines(v.completed) || !lines(v.remaining) || !Array.isArray(v.verifiedTasks) || v.verifiedTasks.length > 100) return;
  if (!v.verifiedTasks.every(t => t && typeof t === 'object' && typeof t.id === 'string'
      && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(t.id)
      && typeof t.evidence === 'string' && t.evidence.trim() && t.evidence.length <= 4096)) return;
  return { completed: v.completed, remaining: v.remaining, verifiedTasks: v.verifiedTasks };
}

/** Never infer task completion from green CI or PR merge alone. */
export function reconcileReviewedTasks(tasks: Task[], closure: ReviewClosure | undefined, head: string): Task[] {
  return tasks.map(task => {
    const verified = closure?.verifiedTasks.find(item => item.id === task.id);
    // Deferred/blocked tasks stay visible; resolving them requires a repair milestone.
    return task.status === 'open' && verified
      ? { ...task, status: 'done', note: `Verified at ${head}: ${verified.evidence}` } : task;
  });
}

export function reviewClosureReport(input: {
  summary: string; tasks: Task[]; closure?: ReviewClosure; merged: boolean; sha: string; blocker?: string;
}): ClosureReport {
  return {
    summary: input.summary,
    completed: input.closure?.completed ?? input.tasks.filter(t => t.status === 'done').map(t => t.title),
    // A narrative must not silently hide unfinished/deferred tasks on the board.
    remaining: [...new Set([...(input.closure?.remaining ?? []),
      ...input.tasks.filter(t => t.status !== 'done').map(t => `${t.title}${t.note ? ` — ${t.note}` : ''}`),
      ...(input.blocker ? [input.blocker] : [])])],
    delivery: input.merged ? 'Pull request merged.' : 'Pull request has not been verified merged.',
    reviewed_revision: input.sha,
    reporting_notes: !input.closure ? ['Detailed closure information was not supplied; the saved checklist is shown.'] : [],
  };
}
