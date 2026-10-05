import type { GraphNode } from '@/types/orchestration';

/** Evaluation issues are stories too; unlinked evaluations are checkpoints. */
export function isEvaluationStory(node: GraphNode): boolean {
  return node.kind === 'eval' && Boolean(node.issue_ref?.trim());
}

export function countStories(nodes: GraphNode[]) {
  const active = nodes.filter((node) => node.state !== 'superseded');
  const stories = active.filter((node) => node.kind === 'story' || isEvaluationStory(node));
  return {
    implementation: active.filter((node) => node.kind === 'story').length,
    evaluation: active.filter(isEvaluationStory).length,
    total: stories.length,
    complete: stories.filter((node) => node.state === 'passed').length,
  };
}
