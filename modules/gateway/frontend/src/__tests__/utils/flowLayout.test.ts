/**
 * Tests for graph-shape derivation — issue #4212.
 *
 * `GraphView.test.tsx` covers this through the DOM, which is the right level for
 * "does a fan-out render as columns". These cover the branch-detection edge cases
 * that are awkward to set up through a page render and easy to get wrong: an edge
 * that crosses a wave boundary, a duplicate edge, and a cycle.
 */
import { describe, it, expect } from 'vitest';
import { groupIntoEpics, blockingPredecessors } from '@/utils/flowLayout';
import type { FlowGraph, GraphNode } from '@/types/orchestration';

function node(overrides: Partial<GraphNode> & { node_ref: string }): GraphNode {
  return {
    id: `id-${overrides.node_ref}`,
    epic_ref: 'epic-1',
    wave_ref: 'wave-1',
    kind: 'story',
    title: `Node ${overrides.node_ref}`,
    state: 'pending',
    stalled: false,
    issue_ref: null,
    attempts: 0,
    cost: { status: 'unknown', amount_usd: null, reason: 'not_started' },
    ...overrides,
  };
}

function graph(nodes: GraphNode[], edges: FlowGraph['edges'] = []): FlowGraph {
  return {
    flow_id: 'flow-1',
    slug: 'delivery-loop',
    title: 'Delivery loop',
    intent_ref: '4120',
    state: 'running',
    nodes,
    edges,
    cost: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows' },
  };
}

describe('groupIntoEpics', () => {
  const edge = (from: GraphNode, to: GraphNode) => ({ from_node_id: from.id, to_node_id: to.id });
  const refs = (wave: ReturnType<typeof groupIntoEpics>[number]['waves'][number]) => wave.stages.map((stage) => stage.map((n) => n.node_ref));

  it('shows parallel stories between a shared gate and evaluation regardless of creation order', () => {
    const start = node({ node_ref: 'start', kind: 'gate' });
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const review = node({ node_ref: 'review', kind: 'eval' });
    const result = groupIntoEpics(graph([review, a, start, b], [edge(start, a), edge(start, b), edge(a, review), edge(b, review)]));
    expect(refs(result[0].waves[0])).toEqual([['start'], ['a', 'b'], ['review']]);
  });

  it('does not add barriers between uneven branches', () => {
    const [a, b, c, x] = ['a', 'b', 'c', 'x'].map((node_ref) => node({ node_ref }));
    const g = graph([c, b, x, a], [edge(a, b), edge(b, c)]);
    expect(refs(groupIntoEpics(g)[0].waves[0])).toEqual([['x', 'a'], ['b'], ['c']]);
    expect(blockingPredecessors(g, b)).toEqual(['Node a']);
  });

  it('records cross-wave dependencies without merging independent work', () => {
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const c = node({ node_ref: 'c', wave_ref: 'wave-2' });
    const [wave1, wave2] = groupIntoEpics(graph([a, b, c], [edge(a, c)]))[0].waves;
    expect(refs(wave1)).toEqual([['a', 'b']]);
    expect(refs(wave2)).toEqual([['c']]);
    expect(wave2.dependsOn).toEqual([{ epicRef: 'epic-1', waveRef: 'wave-1' }]);
  });

  it('follows dependency paths that leave and re-enter the wave', () => {
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const outside = node({ node_ref: 'outside', wave_ref: 'wave-2' });
    const result = groupIntoEpics(graph([b, a, outside], [edge(a, outside), edge(outside, b)]));
    expect(refs(result[0].waves[0])).toEqual([['a'], ['b']]);
  });

  it('handles duplicate edges', () => {
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    expect(refs(groupIntoEpics(graph([a, b], [edge(a, b), edge(a, b)]))[0].waves[0])).toEqual([['a'], ['b']]);
  });

  it('retains cyclic steps without inventing execution order', () => {
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const wave = groupIntoEpics(graph([a, b], [edge(a, b), edge(b, a)]))[0].waves[0];
    expect(wave.stages).toEqual([]);
    expect(wave.unordered).toEqual([a, b]);
  });

  it('does not claim known order when a predecessor is missing', () => {
    const a = node({ node_ref: 'a' });
    const wave = groupIntoEpics(graph([a], [{ from_node_id: 'missing', to_node_id: a.id }]))[0].waves[0];
    expect(wave.stages).toEqual([]);
    expect(wave.unordered).toEqual([a]);
  });

  it('preserves plan wave ordering', () => {
    const result = groupIntoEpics(graph([node({ node_ref: 'a', wave_ref: 'wave-2' }), node({ node_ref: 'b', wave_ref: 'wave-10' })]));
    expect(result[0].waves.map((w) => w.waveRef)).toEqual(['wave-2', 'wave-10']);
  });

  it('returns no EPICs for an empty flow', () => { expect(groupIntoEpics(graph([]))).toEqual([]); });
});

describe('blockingPredecessors', () => {
  it('names only unfinished predecessors', () => {
    const done = node({ node_ref: 'done', title: 'Already done', state: 'passed' });
    const running = node({ node_ref: 'run', title: 'Still going', state: 'running' });
    const target = node({ node_ref: 'target' });
    const g = graph([done, running, target], [
      { from_node_id: done.id, to_node_id: target.id },
      { from_node_id: running.id, to_node_id: target.id },
    ]);

    // Listing the finished one would send an operator to look at completed work.
    expect(blockingPredecessors(g, target)).toEqual(['Still going']);
  });

  it('treats a stalled predecessor as blocking', () => {
    const stalled = node({ node_ref: 's', title: 'Stuck work', state: 'failed', stalled: true });
    const target = node({ node_ref: 'target' });
    const g = graph([stalled, target], [{ from_node_id: stalled.id, to_node_id: target.id }]);

    expect(blockingPredecessors(g, target)).toEqual(['Stuck work']);
  });

  it('returns nothing for a node with no predecessors', () => {
    const solo = node({ node_ref: 'solo' });
    expect(blockingPredecessors(graph([solo]), solo)).toEqual([]);
  });
});
