import type { GraphNode, StoryExecution } from '@/types/orchestration';

/** Display only: current-attempt activity never changes engine state or authority. */
export function withStoryActivity(node: GraphNode, history: StoryExecution | null, now = Date.now()): GraphNode {
  if (history && history.run_id !== node.run_id) return node;
  const activity = history?.activity ?? null;
  const result = { ...node, activity, execution_history: history };
  const progress = node.delivery_progress;
  const wait = activity?.runtime_budget_wait;
  const age = wait ? now - Date.parse(wait.observed_at) : NaN;
  if (!['running', 'awaiting_merge'].includes(node.state) || !history?.history_complete ||
      !activity || activity.status !== 'in_progress' || activity.liveness !== 'live' ||
      !wait || !['budget_wait', 'budget_reservations_pending', 'budget_unavailable'].includes(wait.blocker) ||
      !Number.isFinite(age) || age < 0 || age > 120_000 || !progress || progress.blocker || progress.automation !== 'engine') return result;
  return { ...result, delivery_progress: {
    ...progress, ...wait, blockers: [wait.blocker], automation: 'blocked',
    actor: activity.persona.includes('reviewer') ? 'reviewer' : 'developer',
  } };
}
