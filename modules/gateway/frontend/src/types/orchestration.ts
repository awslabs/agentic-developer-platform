/**
 * Wire types for the orchestration graph view (issue #4212).
 *
 * These mirror `GraphNodeResponse` / `GraphEdgeResponse` / `FlowGraphResponse` in
 * `src/orchestration/routes.py`. Two shapes here are deliberate and load-bearing:
 *
 * **1. Nodes carry address components, never a joined address.** §7.2 of the
 * design contract makes `flow/epic/wave/node` internal — it is the cost join key
 * and must never be rendered. Shipping `epic_ref`/`wave_ref`/`node_ref`
 * separately is both what the view needs (it groups by EPIC and wave to derive
 * its containers) and the shape that does not invite displaying a path.
 *
 * **2. There is no `containers` field, and adding one would be a regression.**
 * §8.2: container state is derived, never stored. The view computes wave and EPIC
 * grouping from the nodes it already has; a server-sent container state would be
 * a second source of truth for a value its children already imply.
 *
 * `cost` is inlined per node rather than fetched from `/cost` and correlated
 * client-side, because correlating would mean the SPA rebuilding the internal
 * address string as a join key — a second implementation of the address format in
 * the layer least able to notice when it drifts.
 */

import type { CostFigure } from '@/utils/cost';

/**
 * The engine's nine states, verbatim from `state.py`.
 *
 * `rejected` and `skipped` are **not** members and never will be (R-N2c): they
 * are phantom states from an earlier draft that the engine raises `ValueError`
 * on. The closest real state is `rejected_at_gate`, which is a different thing —
 * a gate declined a specific attempt, and the node is still live.
 */
export type NodeEngineState =
  | 'pending'
  | 'ready'
  | 'running'
  | 'awaiting_gate'
  | 'passed'
  | 'rejected_at_gate'
  | 'failed'
  | 'halted'
  | 'superseded';

/** The three executable node kinds. Containers are not among them (§8.2). */
export type NodeKind = 'story' | 'eval' | 'gate';

/** An aggregate cost figure — a `CostFigure` plus rollup provenance. */
export interface AggregateCostFigure extends CostFigure {
  total_tokens?: number;
  call_count?: number;
  node_count?: number;
  unknown_node_count?: number;
  /**
   * True when any member node is `unknown`, making the total a **lower bound**
   * rather than a total (AC-21). Rendering a partial total as a total is how
   * budget decisions get made on wrong numbers.
   */
  partial?: boolean;
}

export interface GraphNode {
  id: string;
  epic_ref: string;
  wave_ref: string;
  node_ref: string;
  kind: NodeKind;
  title: string;
  state: NodeEngineState;
  /**
   * Derived server-side from the append-only decision log, not readable from
   * `state`. A stall moves a node to `failed` + a `node_stalled` decision, so
   * `state` alone collapses "stuck, needs a human" into "broke" — the exact
   * distinction AC-3 requires be visible.
   */
  stalled: boolean;
  issue_ref: string | null;
  attempts: number;
  cost: CostFigure;
}

/** A dependency edge, as a pair of node ids. */
export interface GraphEdge {
  from_node_id: string;
  to_node_id: string;
}

export interface FlowGraph {
  flow_id: string;
  slug: string;
  title: string;
  intent_ref: string | null;
  state: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  cost: AggregateCostFigure;
}
