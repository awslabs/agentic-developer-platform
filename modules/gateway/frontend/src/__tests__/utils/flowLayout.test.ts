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
  it('does not merge branches on an edge that leaves the wave', () => {
    // The load-bearing case. `wave-2`'s node depends on `wave-1`'s, which
    // sequences the WAVES. Unioning on that edge would pull cross-wave nodes into
    // one branch set and erase the parallelism inside each wave.
    const a = node({ node_ref: 'a', wave_ref: 'wave-1' });
    const b = node({ node_ref: 'b', wave_ref: 'wave-1' });
    const c = node({ node_ref: 'c', wave_ref: 'wave-2' });
    const result = groupIntoEpics(graph([a, b, c], [{ from_node_id: a.id, to_node_id: c.id }]));

    const [wave1, wave2] = result[0].waves;
    expect(wave1.branches).toHaveLength(2);
    expect(wave2.branches).toHaveLength(1);
  });

  it('is idempotent under a duplicate edge', () => {
    // Hits the `ra === rb` path in union: the second edge finds both nodes
    // already in one set and must be a no-op rather than corrupting the parent
    // array.
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const edge = { from_node_id: a.id, to_node_id: b.id };
    const result = groupIntoEpics(graph([a, b], [edge, edge]));

    expect(result[0].waves[0].branches).toHaveLength(1);
  });

  it('terminates on a cycle rather than hanging', () => {
    // A cycle should not exist in a DAG, but a malformed payload must not spin
    // the render loop forever.
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const result = groupIntoEpics(
      graph([a, b], [
        { from_node_id: a.id, to_node_id: b.id },
        { from_node_id: b.id, to_node_id: a.id },
      ])
    );

    expect(result[0].waves[0].branches).toHaveLength(1);
  });

  it('ignores an edge referencing a node that is not in the payload', () => {
    const a = node({ node_ref: 'a' });
    const result = groupIntoEpics(graph([a], [{ from_node_id: a.id, to_node_id: 'id-missing' }]));

    expect(result[0].waves[0].branches).toHaveLength(1);
  });

  it('chains three nodes into one branch, in dependency order', () => {
    const a = node({ node_ref: 'a' });
    const b = node({ node_ref: 'b' });
    const c = node({ node_ref: 'c' });
    const result = groupIntoEpics(
      graph([a, b, c], [
        { from_node_id: a.id, to_node_id: b.id },
        { from_node_id: b.id, to_node_id: c.id },
      ])
    );

    expect(result[0].waves[0].branches).toHaveLength(1);
    expect(result[0].waves[0].branches[0].map((n) => n.node_ref)).toEqual(['a', 'b', 'c']);
  });

  it('preserves server ordering rather than sorting wave refs lexicographically', () => {
    // A lexicographic sort would put wave-10 before wave-2.
    const result = groupIntoEpics(
      graph([
        node({ node_ref: 'a', wave_ref: 'wave-2' }),
        node({ node_ref: 'b', wave_ref: 'wave-10' }),
      ])
    );

    expect(result[0].waves.map((w) => w.waveRef)).toEqual(['wave-2', 'wave-10']);
  });

  it('returns no EPICs for an empty flow', () => {
    expect(groupIntoEpics(graph([]))).toEqual([]);
  });
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
