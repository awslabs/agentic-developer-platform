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
  | 'no_op'
  // Issue #4020: a loop/validation guard in the Lambda stopped the spawn.
  | 'blocked'
  // Issue #4020: the worker deduplicated a redelivery of already-completed work.
  | 'skipped'
  // Issue #4187: a per-run or per-chain spend cap ended the run. Deliberately
  // not 'failed' — the run was stopped on purpose by a limit that was working.
  | 'budget_stopped'
  // Issue #3964: an operator stopped the run on purpose and the worker confirmed
  // the abort finalized. Terminal, and deliberately not 'failed' for the same
  // reason `budget_stopped` is not: nothing went wrong.
  //
  // Only a confirmed ADP abort finalization carries this status. A provider's
  // native interruption — an SDK cancellation, a dropped connection — is NOT this
  // value; adapters normalize their own outcomes before anything is written, so
  // the frontend never sees a provider string here.
  | 'aborted';

/**
 * Issue #4176: three-value liveness verdict, derived server-side.
 *
 * Distinct from `status`, and rendered *beside* it rather than instead of it:
 *
 * - `live`         — a recent positive signal exists.
 * - `exited`       — a terminal status was actually observed.
 * - `unverifiable` — no positive evidence either way. We could not learn whether
 *                    the run is alive.
 *
 * `unverifiable` is emphatically NOT a claim that the run ended. Loss of contact
 * is not evidence of exit, so nothing in the UI may render it as finished or use
 * it to decide a run can be retried — that mistake orphans live work and can
 * double-dispatch an agent onto an issue that already has one.
 *
 * Optional: rows serialized before the field existed carry no verdict, and the
 * backend may add values on its own cadence, so unknown values must degrade
 * rather than throw.
 */
export type LivenessVerdict = 'live' | 'unverifiable' | 'exited';

/** Channel through which the invocation was triggered. */
export type InvocationChannel = 'github' | 'slack' | 'api' | 'manual';

/** How the invocation was triggered (Phase 6 lineage). */
export type TriggerKind = 'human' | 'agent' | 'bot';

/** A single agent invocation row from the API. */
export interface InvocationItem {
  source_type?: 'activity' | 'task';
  task_id?: string | null;
  task_snapshot?: { task_id: string; invocation_id: string; status: string } | null;
  transcript_kind?: 'task_report' | null;
  transcript_status?: 'available' | 'pending' | 'unavailable' | null;
  invocation_id: string;
  user_id: string;
  persona: string;
  channel: InvocationChannel;
  status: InvocationStatus;
  /** Issue #4176: derived liveness verdict. Null on pre-feature rows. */
  liveness?: LivenessVerdict | null;
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
  /**
   * Issue #4020: static enum explaining why this delivery produced no agent run
   * (no_op / blocked / skipped). Null for runs that dispatched normally and for
   * rows written before the field existed. Render via `describeSkipReason()` —
   * never show the raw enum to the user.
   */
  skip_reason: string | null;
  /**
   * Issue #4187: static enum naming the spend cap that stopped this run, paired
   * with the `budget_stopped` status. Null for every other status. Render via
   * `describeStopReason()` — never show the raw enum to the user.
   */
  stop_reason: string | null;
  // Issue #1653: Run log link (Tier 2 — null until worker persists it)
  run_log_url: string | null;
  // Issue #3069: S3 transcript key (null for pre-#3061 runs or upload failures)
  transcript_key: string | null;
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
  /** Issue #4176: derived liveness verdict. Null on pre-feature rows. */
  liveness?: LivenessVerdict | null;
  topic: string | null;
  persona: string | null;
  parent_invocation_id: string | null;
  children: InvocationChainItem[];
  // Issue #3069: S3 transcript key
  transcript_key: string | null;
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

/** Issue #1662: A chain summary — root run + descendants + chain-level aggregates. */
export interface ChainSummary {
  chain_id: string;
  root: InvocationItem;
  descendant_count: number;
  descendants: InvocationChainItem[];
  chain_total_cost_usd: number | null;
  chain_total_tokens: number | null;
  chain_total_call_count: number | null;
}

/** Issue #1662: Paginated list of chains for the chain-grouped board view. */
export interface ChainListResponse {
  chains: ChainSummary[];
  count: number;
  last_key: string | null;
}

/** Query parameters for fetching invocations. */
export interface InvocationQueryParams {
  status?: InvocationStatus;
  channel?: InvocationChannel;
  persona?: string;
  /**
   * Issue #4390: these three mirror the backend query-param names exactly
   * (`src/activity/routes.py`). They were previously named start_date/end_date/
   * limit, which FastAPI silently dropped — the filters were inert.
   * A bare YYYY-MM-DD is fine: the server widens it to a full-day instant.
   */
  since?: string;
  until?: string;
  page_size?: number;
  last_key?: string;
  /**
   * Issue #1658: When false (default), exclude non-triggering rows —
   * webhook_received plus the non-run statuses (no_op, and since #4020 also
   * blocked and skipped).
   */
  include_non_triggering?: boolean;
  /** Issue #1662: View mode — 'runs' (flat list) or 'chains' (grouped by chain). */
  view?: 'runs' | 'chains';
}
