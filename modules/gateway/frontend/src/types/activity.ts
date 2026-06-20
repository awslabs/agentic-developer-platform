/**
 * Types for the Agent Activity page.
 *
 * Issue #1457: Frontend "Agent Activity" page — Phase 3 of Agent Activity rollout.
 * Issue #1461: Phase 6 — lineage fields (trigger_kind, parent, chain view).
 * Mirrors the Phase 2 read API response contract.
 */

/** Status lifecycle of an agent invocation. */
export type InvocationStatus =
  | 'webhook_received'
  | 'in_progress'
  | 'complete'
  | 'failed'
  | 'rejected'
  | 'rate_limited'
  | 'no_op';

/** Channel through which the invocation was triggered. */
export type InvocationChannel = 'github' | 'slack' | 'api' | 'manual';

/** How the invocation was triggered (Phase 6 lineage). */
export type TriggerKind = 'human' | 'agent' | 'bot';

/** A single agent invocation row from the API. */
export interface InvocationItem {
  invocation_id: string;
  user_id: string;
  persona: string;
  channel: InvocationChannel;
  status: InvocationStatus;
  topic: string | null;
  summary: string | null;
  source_url: string | null;
  repo: string | null;
  issue_number: number | null;
  invoked_at: string;
  completed_at: string | null;
  status_updated_at: string | null;
  run_id: string | null;
  // Phase 6 lineage fields (#1461)
  trigger_kind: TriggerKind;
  triggered_by_invocation_id: string | null;
  triggered_by_topic: string | null;
  root_human_id: string | null;
  is_human_rooted: boolean;
  correlation_id: string | null;
  // Issue #1616: Per-run cost fields
  total_cost_usd: number | null;
  total_tokens: number | null;
  call_count: number | null;
  // Error detail surfaced in the row-detail view for failed invocations
  error_message: string | null;
  // Issue #1653: Run log link (Tier 2 — null until worker persists it)
  run_log_url: string | null;
}

/** Cursor-paginated response from GET /me/agent-invocations or /admin/agent-invocations. */
export interface InvocationListResponse {
  items: InvocationItem[];
  last_key: string | null;
}

/** A node in the invocation chain tree. */
export interface InvocationChainItem {
  invocation_id: string;
  invoked_at: string;
  channel: string | null;
  status: string | null;
  topic: string | null;
  persona: string | null;
  parent_invocation_id: string | null;
  children: InvocationChainItem[];
  // Issue #1653: Per-node cost
  total_cost_usd: number | null;
  total_tokens: number | null;
  call_count: number | null;
}

/** Response from GET /me/agent-invocations/chain/{correlation_id}. */
export interface InvocationChainResponse {
  correlation_id: string;
  root_human_id: string | null;
  is_human_rooted: boolean;
  items: InvocationChainItem[];
  total_count: number;
  depth_capped: boolean;
  // Issue #1653: Chain-wide cost totals
  chain_total_cost_usd: number | null;
  chain_total_tokens: number | null;
  chain_total_call_count: number | null;
}

/** Query parameters for fetching invocations. */
export interface InvocationQueryParams {
  status?: InvocationStatus;
  channel?: InvocationChannel;
  persona?: string;
  start_date?: string;
  end_date?: string;
  limit?: number;
  last_key?: string;
}
