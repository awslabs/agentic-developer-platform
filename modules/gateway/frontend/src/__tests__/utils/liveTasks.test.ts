import { describe, expect, it } from 'vitest';
import { mentionsTask, parseLiveTasks } from '@/utils/liveTasks';
import type { LiveExplanation } from '@/services/agentExplanations';
const board = '**Workspace**\n- ☑ `code` WS-c1 — Create workspace\n- ▶ `test` WS-t1 — Check readiness (covers WS-c1)\n- ⛔ `infra` WS-i1 — Live check — Waiting for an account\n- ☐ `test` WS-t2 — Check membership';
describe('live task board', () => {
  it('keeps blocked tasks in totals and preserves their explanation', () => {
    const tasks = parseLiveTasks(board)!;
    expect(tasks).toHaveLength(4);
    expect(tasks.map(task => task.status)).toEqual(['completed', 'in_progress', 'blocked', 'pending']);
    expect(tasks[2].description).toContain('Waiting for an account');
    expect(tasks[0].group).toBe('Workspace');
  });
  it('falls back rather than presenting partial or duplicate task totals', () => {
    expect(parseLiveTasks(board + '\n- ☐ Generic task without an ID')).toBeUndefined();
    expect(parseLiveTasks(board + '\n- ☑ `code` WS-c1 — Duplicate')).toBeUndefined();
    expect(parseLiveTasks('```md\n' + board + '\n```')).toBeUndefined();
  });
  it('matches explicit IDs without confusing similarly named tasks or counting the checklist', () => {
    const event = (text: string, category: 'message' | 'plan' = 'message') => ({ payload: { text, progress: { category } } }) as LiveExplanation;
    expect(mentionsTask(event('Verified `WS-c1` and WS-t1.'), 'WS-c1')).toBe(true);
    expect(mentionsTask(event('Verified `WS-c1` and WS-t1.'), 'WS-t1')).toBe(true);
    for (const text of ['WS-c10', 'WS-c1-more', 'WS-c1.extra', 'prefixWS-c1', 'Running tests']) expect(mentionsTask(event(text), 'WS-c1')).toBe(false);
    expect(mentionsTask(event(board, 'plan'), 'WS-c1')).toBe(false);
    expect(mentionsTask(event('WS.c1'), 'WS.c1')).toBe(true);
    expect(mentionsTask(event('WSxc1'), 'WS.c1')).toBe(false);
  });
});
