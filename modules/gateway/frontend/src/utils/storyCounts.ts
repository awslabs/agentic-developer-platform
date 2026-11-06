import type { GraphNode } from '@/types/orchestration';

/** Evaluation issues are stories too; unlinked evaluations are checkpoints. */
export function isEvaluationStory(node: GraphNode): boolean {
  return node.kind === 'eval' && Boolean(node.issue_ref?.trim());
}

export function countStories(nodes: GraphNode[], combinePhases = false) {
  const active = nodes.filter((node) => node.state !== 'superseded');
  const stories = active.filter((node) => node.kind === 'story' || isEvaluationStory(node));
  if (combinePhases) {
    const groups = new Map<string, GraphNode[]>();
    const waveKeys = new Set(active.map((node) => JSON.stringify([node.epic_ref, node.wave_ref])));
    for (const node of stories) {
      // Only BOLT build/live pairs share a display identity. Ordinary waves and
      // unlinked nodes retain their existing counting behavior.
      const bolt = /^(b\d+)-(build|live)$/.exec(node.wave_ref)?.[1];
      const paired = bolt && waveKeys.has(JSON.stringify([node.epic_ref, `${bolt}-${node.wave_ref.endsWith('-build') ? 'live' : 'build'}`]))
        && !waveKeys.has(JSON.stringify([node.epic_ref, `bolt-${bolt}`]));
      const issue = node.issue_url || node.issue_ref?.trim();
      const key = paired && issue ? JSON.stringify([node.epic_ref, bolt, node.kind, issue]) : node.id;
      groups.set(key, [...(groups.get(key) ?? []), node]);
    }
    const grouped = [...groups.values()];
    return {
      implementation: grouped.filter((phases) => phases[0].kind === 'story').length,
      evaluation: grouped.filter((phases) => isEvaluationStory(phases[0])).length,
      total: grouped.length,
      complete: grouped.filter((phases) => phases.every((node) => node.state === 'passed')).length,
    };
  }
  return {
    implementation: active.filter((node) => node.kind === 'story').length,
    evaluation: active.filter(isEvaluationStory).length,
    total: stories.length,
    complete: stories.filter((node) => node.state === 'passed').length,
  };
}
