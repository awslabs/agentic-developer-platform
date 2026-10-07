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

it('reads stable parent metadata and keeps an unfinished parent incomplete', async () => {
  const { implementationSteps } = await import('@/utils/liveTasks');
  const tasks = parseLiveTasks('**⛔ Plan `recover` — Keep conversations after disconnects**\n- ☑ `code` A-c1 — Save conversation\n- ⛔ `test` A-t1 — Await live target')!;
  expect(tasks[0].planStep?.id).toBe('recover');
  expect(implementationSteps(tasks)[0]).toMatchObject({ id: 'recover', completed: 1, status: 'blocked' });
  expect(implementationSteps(tasks.slice(0, 1))[0].status).not.toBe('completed');
});
