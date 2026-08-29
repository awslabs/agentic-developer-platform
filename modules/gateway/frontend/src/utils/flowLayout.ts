/**
 * Deriving the graph's shape from nodes + edges (issue #4212).
 *
 * Kept out of the component because two things here are real graph logic that
 * needs its own tests: parallel-branch detection (AC-2) and container derivation
 * (§8.2). Buried in JSX they would only ever be tested through the DOM.
 *
 * **Containers are derived here, never received** (§8.2). The API sends executable
 * nodes only; EPIC and wave grouping is computed from `epic_ref`/`wave_ref`. That
 * keeps one source of truth — a server-sent container state would duplicate a
 * value its children already imply, and the two would eventually disagree.
 *
 * **Parallel means "same wave, no path between them"** (AC-2). Sharing a wave is
 * not sufficient on its own: two nodes in one wave can still be sequenced by an
 * explicit edge, and drawing those side by side would claim concurrency the engine
 * will not deliver. So the branch split is computed from the edges within the
 * wave, and a wave whose nodes form a chain renders as one column.
 */

import type { FlowGraph, GraphNode } from '@/types/orchestration';
import { toDisplayState } from './nodeState';

/** A wave: an ordered set of parallel branches within one EPIC. */
export interface WaveGroup {
  waveRef: string;
  /**
   * Independent branches. `length > 1` is the AC-2 case — these render as
   * distinct columns. Each inner array is one chain, in dependency order.
   */
  branches: GraphNode[][];
}

export interface EpicGroup {
  epicRef: string;
  waves: WaveGroup[];
}

/**
 * Split one wave's nodes into branches that can genuinely run concurrently.
 *
 * Two nodes are in the same branch when an edge chains them (in either
 * direction — union-find over the wave's internal edges). Nodes with no
 * intra-wave edge to anything are each their own branch, which is the common
 * fan-out case.
 */
function splitIntoBranches(nodes: GraphNode[], edges: FlowGraph['edges']): GraphNode[][] {
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  const parent = nodes.map((_, i) => i);

  const find = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]];
      i = parent[i];
    }
    return i;
  };
  const union = (a: number, b: number): void => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent[rb] = ra;
  };

  for (const edge of edges) {
    const from = index.get(edge.from_node_id);
    const to = index.get(edge.to_node_id);
    // Only edges with BOTH ends inside this wave chain nodes here. An edge
    // leaving the wave sequences waves, not branches, and unioning on it would
    // merge every branch into one and erase the parallelism.
    if (from !== undefined && to !== undefined) union(from, to);
  }

  const byRoot = new Map<number, GraphNode[]>();
  nodes.forEach((node, i) => {
    const root = find(i);
    const bucket = byRoot.get(root);
    if (bucket) bucket.push(node);
    else byRoot.set(root, [node]);
  });

  return [...byRoot.values()];
}

/**
 * Group a flow's nodes into EPICs → waves → parallel branches.
 *
 * Ordering is **first-appearance** within each level, which preserves the
 * server's ordering (the repository returns nodes in creation order, i.e.
 * plan order) without this module inventing a sort. Sorting `wave_ref`
 * lexicographically would put `wave-10` before `wave-2`.
 */
export function groupIntoEpics(graph: FlowGraph): EpicGroup[] {
  const epics = new Map<string, Map<string, GraphNode[]>>();

  for (const node of graph.nodes) {
    let waves = epics.get(node.epic_ref);
    if (!waves) {
      waves = new Map();
      epics.set(node.epic_ref, waves);
    }
    const bucket = waves.get(node.wave_ref);
    if (bucket) bucket.push(node);
    else waves.set(node.wave_ref, [node]);
  }

  return [...epics.entries()].map(([epicRef, waves]) => ({
    epicRef,
    waves: [...waves.entries()].map(([waveRef, nodes]) => ({
      waveRef,
      branches: splitIntoBranches(nodes, graph.edges),
    })),
  }));
}

/**
 * Titles of a node's predecessors that have not finished yet.
 *
 * Feeds the "Waiting on X" caption on queued chips. Only *unfinished*
 * predecessors are named: listing a completed one would tell an operator to go
 * look at work that is already done.
 */
export function blockingPredecessors(graph: FlowGraph, node: GraphNode): string[] {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  return graph.edges
    .filter((edge) => edge.to_node_id === node.id)
    .map((edge) => byId.get(edge.from_node_id))
    .filter((predecessor): predecessor is GraphNode => {
      if (!predecessor) return false;
      return toDisplayState(predecessor) !== 'complete';
    })
    .map((predecessor) => predecessor.title);
}
