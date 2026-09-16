/** Dependency order for the delivery flow. Wave containers never schedule work. */
import type { FlowGraph, GraphNode } from '@/types/orchestration';
import { toDisplayState } from './nodeState';

export interface WaveGroup {
  waveRef: string;
  nodes: GraphNode[];
  /** Each group is an antichain: no dependency path connects its members. */
  stages: GraphNode[][];
  /** Malformed dependencies stay visible, without suggesting execution order. */
  unordered: GraphNode[];
  dependsOn: { epicRef: string; waveRef: string }[];
}

export interface EpicGroup {
  epicRef: string;
  waves: WaveGroup[];
}

function dependencyStages(nodes: GraphNode[], graph: FlowGraph): Pick<WaveGroup, 'stages' | 'unordered'> {
  const ids = new Set(nodes.map((node) => node.id));
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const predecessors = new Map<string, Set<string>>();
  for (const edge of graph.edges) {
    if (!predecessors.has(edge.to_node_id)) predecessors.set(edge.to_node_id, new Set());
    predecessors.get(edge.to_node_id)!.add(edge.from_node_id);
  }

  // Follow paths through other waves too. A → outside this wave → B still
  // sequences A and B; looking only at internal edges would claim parallelism.
  const dependencies = new Map<string, Set<string>>();
  for (const node of nodes) {
    const local = new Set<string>();
    const visited = new Set<string>();
    const pending = [...(predecessors.get(node.id) ?? [])];
    while (pending.length) {
      const id = pending.pop()!;
      if (visited.has(id)) continue;
      visited.add(id);
      if (!byId.has(id)) return { stages: [], unordered: nodes };
      if (ids.has(id)) local.add(id);
      else pending.push(...(predecessors.get(id) ?? []));
    }
    dependencies.set(node.id, local);
  }

  const stages: GraphNode[][] = [];
  let remaining = nodes;
  const done = new Set<string>();
  while (remaining.length) {
    const ready = remaining.filter((node) => [...dependencies.get(node.id)!].every((id) => done.has(id)));
    if (!ready.length) break; // Cycle: retain the remainder without parallel labels.
    stages.push(ready);
    ready.forEach((node) => done.add(node.id));
    remaining = remaining.filter((node) => !done.has(node.id));
  }
  return { stages, unordered: remaining };
}

/** Preserve the plan's wave order; sort steps by dependency, not creation time. */
export function groupIntoEpics(graph: FlowGraph): EpicGroup[] {
  const epics = new Map<string, Map<string, GraphNode[]>>();
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
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
    waves: [...waves.entries()].map(([waveRef, nodes]) => {
      const ids = new Set(nodes.map((node) => node.id));
      const external = new Map<string, { epicRef: string; waveRef: string }>();
      for (const edge of graph.edges) {
        if (!ids.has(edge.to_node_id) || ids.has(edge.from_node_id)) continue;
        const from = byId.get(edge.from_node_id);
        if (from) external.set(JSON.stringify([from.epic_ref, from.wave_ref]), { epicRef: from.epic_ref, waveRef: from.wave_ref });
      }
      return { waveRef, nodes, ...dependencyStages(nodes, graph), dependsOn: [...external.values()] };
    }),
  }));
}

export function nodeDependencies(graph: FlowGraph, node: GraphNode): GraphNode[] {
  const ids = new Set(graph.edges.filter((edge) => edge.to_node_id === node.id).map((edge) => edge.from_node_id));
  return graph.nodes.filter((candidate) => ids.has(candidate.id));
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
