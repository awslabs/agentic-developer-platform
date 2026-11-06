import { describe, expect, it } from 'vitest';
import type { GraphNode, StoryExecution } from '@/types/orchestration';
import { withStoryActivity } from '@/utils/runtimeBudgetWait';

const now = Date.parse('2026-10-18T02:00:00Z');
const node: GraphNode = {
  id: 'story', epic_ref: 'epic', wave_ref: 'wave', node_ref: 'story', kind: 'story',
  title: 'Story', state: 'awaiting_merge', stalled: false, issue_ref: null, attempts: 2,
  run_id: 'current-attempt', cost: { status: 'unknown', amount_usd: null },
  delivery_progress: {
    stage: 'awaiting_review', actor: 'reviewer', detail: 'Review is running',
    automation: 'engine', blockers: [], next_check_at: '2026-10-18T02:01:00Z',
    scheduled_action: 'Reconcile execution',
  },
};
const history: StoryExecution = {
  run_id: 'current-attempt', history_complete: true, runs: [],
  activity: {
    invocation_id: 'reviewer', persona: 'agent-codex-reviewer', status: 'in_progress', liveness: 'live',
    runtime_budget_wait: {
      blocker: 'budget_reservations_pending', detail: 'Waiting for model budget admission',
      next_action: 'Wait for reconciliation', observed_at: new Date(now).toISOString(),
    },
  },
};

describe('current-attempt budget wait display', () => {
  it('overlays the diagnosis, retains the schedule and clears on refreshed admission', () => {
    const waiting = withStoryActivity(node, history, now);
    expect(waiting.delivery_progress).toMatchObject({
      blocker: 'budget_reservations_pending', automation: 'blocked', actor: 'reviewer',
      next_check_at: node.delivery_progress!.next_check_at,
    });
    expect(node.delivery_progress!.blocker).toBeUndefined();
    const resumed = withStoryActivity(node, {
      ...history, activity: { ...history.activity!, runtime_budget_wait: null },
    }, now);
    expect(resumed.delivery_progress).toBe(node.delivery_progress);
    expect(resumed.activity!.runtime_budget_wait).toBeNull();
  });

  it.each([
    { ...history, run_id: 'previous-attempt' },
    { ...history, history_complete: false },
    { ...history, activity: { ...history.activity!, status: 'complete' } },
    { ...history, activity: { ...history.activity!, liveness: 'exited' as const } },
    ...['unrecognized', 'not_waiting'].map(blocker => ({
      ...history, activity: { ...history.activity!, runtime_budget_wait: { ...history.activity!.runtime_budget_wait!, blocker } },
    })),
  ])('ignores non-current wait observations: %j', (observation) => {
    expect(withStoryActivity(node, observation, now).delivery_progress).toBe(node.delivery_progress);
  });

  it.each([-1, 120_001, NaN])('expires wait observations at age %s ms', age => {
    expect(withStoryActivity(node, history, now + age).delivery_progress).toBe(node.delivery_progress);
  });

  it.each(['budget_exhausted', 'authority_unverifiable', 'human_input_required'])('preserves %s', blocker => {
    const blocked = { ...node, delivery_progress: { ...node.delivery_progress!, blocker, blockers: [blocker] } };
    expect(withStoryActivity(blocked, history, now).delivery_progress).toBe(blocked.delivery_progress);
  });

  it.each(['passed', 'halted', 'pending'] as const)('does not project a wait for a %s story', state => {
    expect(withStoryActivity({ ...node, state }, history, now).delivery_progress).toBe(node.delivery_progress);
  });
});
