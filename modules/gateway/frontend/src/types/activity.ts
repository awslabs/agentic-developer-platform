/**
 * Types for the Agent Activity page.
 *
 * Issue #1457: Frontend "Agent Activity" page — Phase 3 of Agent Activity rollout.
 * Issue #1459: Phase 5 — Row detail + polish. Added detail fields.
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
  /** ISO 8601 timestamp of the last status transition. */
  status_updated_at: string | null;
  /** Correlation ID linking related invocations/events. */
  correlation_id: string | null;
  /** GitHub Actions run ID or job ID (if applicable). */
  run_id: string | null;
  /** Error message for failed invocations (sanitized for end users). */
  error_message: string | null;
}

/** Cursor-paginated response from GET /me/agent-invocations or /admin/agent-invocations. */
export interface InvocationListResponse {
  items: InvocationItem[];
  last_key: string | null;
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
