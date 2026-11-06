import type { FlowGraph, GraphNode } from '@/types/orchestration';

/**
 * The gates a human still has to answer, with where and since when.
 *
 * Gates in a flow are usually chained: approving one lets the engine present the
 * next within a minute, and the next one looks exactly like the one just answered
 * — same "waiting on you" badge, same controls. Naming the gate, its wave and the
 * moment it was presented is what tells an operator that the approval they just
 * gave was recorded and that this is a different decision.
 */
export interface OpenGate {
  node: GraphNode;
  waveTitle: string;
  /** ISO timestamp the gate was presented, when the server reports it. */
  since: string | null;
}

export function openGates(graph: Pick<FlowGraph, 'nodes' | 'wave_metadata'>): OpenGate[] {
  return graph.nodes
    .filter((node) => node.state === 'awaiting_gate')
    .map((node) => ({
      node,
      waveTitle: graph.wave_metadata?.find((item) => item.epic_ref === node.epic_ref && item.wave_ref === node.wave_ref)?.title || node.wave_ref,
      since: node.updated_at ?? null,
    }));
}

/** A short local time for "since" and "at" phrases; the full timestamp stays in the title attribute. */
export function shortTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
