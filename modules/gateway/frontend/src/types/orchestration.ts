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
  | 'awaiting_merge'
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
  run_id?: string | null;
  issue_url?: string | null;
  result_summary?: string | null;
  configuration_problem?: string | null;
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

// ---------------------------------------------------------------------------
// Issue #4213: gate-decision and resume control results.
// ---------------------------------------------------------------------------

/**
 * `actor_kind` — the human-vs-service discriminator, as its own field.
 *
 * It is a wire field rather than something inferred from `actor_id` because the
 * older `tenant_access_requests.decided_by` column mixes real Cognito subs with
 * synthetic values like `system:org-member-match` in one string, leaving "was
 * this approved by a human?" unanswerable after the fact. Here it is explicit.
 */
export type ActorKind = 'human' | 'service';

/**
 * The outcome of answering a gate, mirroring `GateDecisionResponse`.
 *
 * `decision_id` is the append-only row the decision produced. It is null only for
 * `already_answered`, where someone else's decision stands and a second row would
 * claim the gate was answered twice.
 */
export interface GateDecisionResult {
  node_id: string;
  status: string;
  state: NodeEngineState | null;
  decision_id: string | null;
  actor_kind: ActorKind | null;
  message: string;
}

/** The outcome of a resume, mirroring `ResumeResponse`. */
export interface ResumeResult {
  node_id: string;
  from_state: NodeEngineState;
  state: NodeEngineState;
  decision_id: string;
  actor_kind: ActorKind;
}

// ---------------------------------------------------------------------------
// Issue #4869: the flows list. Mirrors `FlowListResponse` in `routes.py`.
// ---------------------------------------------------------------------------

/**
 * The one-word answer to "what is happening with this flow".
 *
 * Six members, not five. `empty` is its own status because a flow whose plan
 * compiled to no nodes is not "queued" — nothing is waiting on anything, and
 * rendering it as queued would have an operator waiting for work that will never
 * start.
 *
 * Server-derived from the flow's nodes (`derive_flow_status` in
 * `display_state.py`). It is **not** `OrchestrationFlow.state`, which has no
 * writer anywhere and is permanently `"pending"` — the API deliberately does not
 * send it.
 */
export type FlowStatus =
  | 'attention_needed'
  | 'awaiting_you'
  | 'running'
  | 'queued'
  | 'complete'
  | 'empty';

/**
 * Node counts in the five-value display vocabulary (§1.3, via `DisplayState`).
 *
 * Always all five keys including zeroes, so a rollup segment renders empty rather
 * than disappearing. There is no `superseded` key: a superseded attempt was
 * replaced by another node and counting it would make one piece of work appear
 * twice.
 */
export interface FlowDisplayCounts {
  queued: number;
  in_progress: number;
  gate: number;
  stalled: number;
  complete: number;
}

/**
 * One wave's rollup, for the rail on a flow card.
 *
 * **Array order is the contract.** The server orders waves by first appearance
 * (`MIN(node.created_at)`), not by `wave_ref` — `wave-10` sorts before `wave-2`
 * lexicographically, and the rail must render them in the order the plan runs
 * them. So the rail maps this array as given and never re-sorts it.
 */
export interface WaveSummary {
  epic_ref: string;
  wave_ref: string;
  total: number;
  done: number;
  display_counts: FlowDisplayCounts;
}

/**
 * The five AIDLC design gates, in the order they run.
 *
 * Mirrors `DESIGN_STAGES` in `src/orchestration/proposal.py`, which validates
 * these names at write time — so an unknown name never reaches this type. Used to
 * render the strip in canonical order and to say "N of 5", both of which must not
 * depend on the order the author happened to list them in.
 */
export const DESIGN_STAGES = [
  'intent-capture',
  'reverse-engineering',
  'requirements-analysis',
  'delivery-planning',
  'loop-proposal',
] as const;

export type DesignStageName = (typeof DESIGN_STAGES)[number];

/**
 * `skipped` and `not_reached` are **different states and must not be merged.**
 *
 * `skipped` means the loop's scope decided this gate never runs — a `poc` scope
 * skips reverse-engineering, and that is the plan working as intended.
 * `not_reached` means the gate will run and the loop has not got there yet.
 * Rendering the first as the second shows an operator outstanding work that is
 * never coming.
 */
export type DesignStageState = 'approved' | 'open' | 'skipped' | 'not_reached';

/** One design gate's outcome. `approved_at` is set only when `state` is `approved`. */
export interface DesignStage {
  name: DesignStageName;
  state: DesignStageState;
  approved_at?: string | null;
}

/**
 * How a loop's design was arrived at — which gates ran, and under which scope.
 *
 * `stages` may be partial: an author records only what they know (#4885), and a
 * stage they cannot establish is omitted rather than guessed. So a consumer must
 * treat an absent entry as "not recorded", never as a state.
 */
export interface DesignHistory {
  scope: 'auto' | 'poc' | 'workshop';
  stages: DesignStage[];
}

/** One flow as the list page reads it: identity plus everything derived. */
export interface FlowSummary {
  id: string;
  slug: string;
  title: string;
  intent_ref: string | null;
  /**
   * The loop's purpose in plain language, capped at 500 chars server-side (#4885).
   *
   * `null` means nobody recorded one — true for every flow registered before the
   * field existed, and for any hand-authored proposal. Render nothing rather than
   * an empty line: absence is not a blank description.
   */
  description: string | null;
  /**
   * The design loop's stage record, or `null` when it was never captured.
   *
   * **`null` must render no stage strip at all** — not an empty one, and not five
   * pending gates. A fabricated strip claims gates that may never have happened and
   * looks authoritative, which is worse than saying nothing.
   */
  design_history: DesignHistory | null;
  status: FlowStatus;
  /**
   * Surfaced alongside `status` because `status` is first-match-wins: a flow that
   * is both stalled and gated reports `attention_needed`, and the card still has
   * to be able to say "1 waiting on you".
   */
  awaiting_gate_count: number;
  /**
   * Decision-derived (latest `node_stalled` wins), **not** the count of `failed`
   * nodes — stall detection writes `failed`, so a stall and a plain failure share
   * an engine state.
   */
  stalled_count: number;
  display_counts: FlowDisplayCounts;
  total_nodes: number;
  epic_count: number;
  wave_count: number;
  /** The first wave with unfinished work; null when everything is done. */
  current_wave_ref: string | null;
  waves: WaveSummary[];
  /** Three-valued. `unknown` carries no amount, so absence cannot render `$0.00`. */
  delivery_cost: CostFigure;
  created_at: string;
  updated_at: string | null;
}

/**
 * A page of flows, the filtered total, and the unfiltered status chips.
 *
 * `total` counts rows matching the **filters across all pages** — not
 * `flows.length`. `status_counts` is unfiltered and tenant-wide, which is why it
 * is a separate number: with a filter on, the summary reads "Showing 3 of 5"
 * while the chips still total 5, because the chips describe the population the
 * operator is choosing among.
 */
export interface FlowList {
  flows: FlowSummary[];
  total: number;
  limit: number;
  offset: number;
  /** Keyed by `FlowStatus`, always all six, including zeroes. */
  status_counts: Record<FlowStatus, number>;
}

/** Query parameters for the flows list. Mirrors the route's signature. */
export interface FlowListParams {
  limit?: number;
  offset?: number;
  /** Substring, case-insensitive, over title / slug / intent_ref. */
  q?: string;
  status?: FlowStatus;
  /** `gate > 0 OR stalled_count > 0` — not an alias of `status`. */
  needs_me?: boolean;
  /** No `cost`: it lives in another table and cannot be sorted with the page. */
  sort?: 'created' | 'updated' | 'stalled';
}
