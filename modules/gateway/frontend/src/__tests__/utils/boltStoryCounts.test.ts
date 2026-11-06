import { describe, expect, it } from 'vitest';
import { countStories } from '@/utils/storyCounts';
import type { GraphNode } from '@/types/orchestration';

function story(id: string, wave: string, state: GraphNode['state'], issue = '42'): GraphNode {
  return { id, node_ref: id, epic_ref: 'epic-1', wave_ref: wave, kind: 'story', title: id,
    issue_ref: issue, state, stalled: false, attempts: 0, cost: { status: 'unknown', amount_usd: null } };
}

describe('BOLT story completion', () => {
  it('counts a shared issue once and waits for every phase before counting it complete', () => {
    const build = story('build', 'b01-build', 'passed');
    const run = story('run', 'b01-live', 'pending');
    expect(countStories([build, run], true)).toEqual({ implementation: 1, evaluation: 0, total: 1, complete: 0 });
    expect(countStories([build, { ...run, state: 'passed' }], true).complete).toBe(1);
    expect(countStories([build, { ...run, state: 'waived' }], true).complete).toBe(0);
  });

  it('does not combine different BOLTs, repositories, epics or ordinary waves', () => {
    const nodes = [story('a', 'b01-build', 'passed'), story('b', 'b01-live', 'pending')];
    expect(countStories(nodes, false).total).toBe(2);
    expect(countStories([nodes[0], { ...nodes[1], epic_ref: 'epic-2' }], true).total).toBe(2);
    expect(countStories([nodes[0], { ...nodes[1], wave_ref: 'b02-live' }], true).total).toBe(2);
    expect(countStories(nodes.map((node, i) => ({ ...node, issue_url: `https://github.com/example/repo-${i}/issues/42` })), true).total).toBe(2);
    expect(countStories(nodes.map((node, i) => ({ ...node, wave_ref: `wave-${i}` })), true).total).toBe(2);
  });

  it('keeps unlinked stories and colliding wave groups separate', () => {
    const nodes = [story('build', 'b01-build', 'passed'), story('run', 'b01-live', 'pending')];
    expect(countStories(nodes.map((node) => ({ ...node, issue_ref: null })), true).total).toBe(2);
    expect(countStories([...nodes, story('other', 'bolt-b01', 'pending')], true).total).toBe(3);
  });
});
