/**
 * Orchestration read API client (issue #4212).
 *
 * **Path prefix.** The route is `/orchestration/...`, NOT `/api/orchestration/...`.
 * `apiClient` prepends `VITE_API_URL` (`/api` in every deployed environment), and
 * CloudFront's `strip-api-prefix` viewer function removes that leading `/api`
 * before the request reaches the ALB — so the backend router mounts at
 * `/orchestration`. Spelling `/api` here produces `/api/api/orchestration`, which
 * misses the SPA fallback and returns HTML with a 200 (issue #4330).
 */

import { apiClient, buildQueryString } from './api';
import type {
  FlowExecution,
  FlowExecutionParams,
  FlowGraph,
  FlowList,
  FlowListParams,
  GateDecisionResult,
  ResumeResult,
} from '@/types/orchestration';

/**
 * Fetch a whole flow for the graph view: every node including ones that have
 * never run, every edge, and per-node plus rolled-up cost.
 *
 * A `flow_id` belonging to another org returns 404, not 403 — a 403 would confirm
 * the id exists somewhere and let a caller enumerate flows by status code.
 */
export async function getFlowGraph(flowId: string): Promise<FlowGraph> {
  return apiClient.get<FlowGraph>(`/orchestration/flows/${encodeURIComponent(flowId)}`);
}

/**
 * Approve a gate: `awaiting_gate -> passed` (issue #4213, AC-5).
 *
 * The body carries only an optional reason. It deliberately cannot carry
 * `actor_kind` — attribution is derived server-side from the authenticated
 * session, and the request model forbids extra fields, so a body asserting
 * `actor_kind` is a 422 rather than a silently-honoured claim.
 */
export async function approveGate(gateId: string, reason?: string): Promise<GateDecisionResult> {
  return apiClient.post<GateDecisionResult>(
    `/orchestration/gates/${encodeURIComponent(gateId)}/approve`,
    { reason: reason ?? null }
  );
}

/** Reject a gate: `awaiting_gate -> rejected_at_gate` (AC-6). */
export async function rejectGate(gateId: string, reason?: string): Promise<GateDecisionResult> {
  return apiClient.post<GateDecisionResult>(
    `/orchestration/gates/${encodeURIComponent(gateId)}/reject`,
    { reason: reason ?? null }
  );
}

/**
 * Resume a stalled (`failed`) or halted node back to `ready` (AC-9).
 *
 * Both edges are human-only in the engine's transition table. Resuming does not
 * increment `attempts`: the cycle bound is spent when the node actually runs, and
 * charging it here would immediately re-halt a node resumed from `halted`.
 */
export async function resumeNode(nodeId: string, reason?: string): Promise<ResumeResult> {
  return apiClient.post<ResumeResult>(
    `/orchestration/nodes/${encodeURIComponent(nodeId)}/resume`,
    { reason: reason ?? null }
  );
}

/**
 * List the caller's org's delivery flows (issue #4869).
 *
 * The entry point the engine had none of: the graph view is addressable only by
 * flow id, so before this a flow nobody had the id for was invisible along with
 * every gate waiting on a human.
 *
 * **Filtering and paging happen server-side, and that is load-bearing.** The
 * status and needs-me predicates are derived from node and decision aggregates,
 * so a client cannot compute them from a page it already holds — and filtering a
 * page client-side would show "3 flows need you" out of the 25 that happened to
 * be fetched, which is a lie about the rest of the list.
 *
 * `limit` over 100 is a 422 rather than a silent clamp, so a caller cannot
 * believe it received 500 rows and page as though it had.
 */
export async function listFlows(params: FlowListParams = {}): Promise<FlowList> {
  // `buildQueryString` drops undefined/null/'' — so an unset filter is absent
  // from the URL rather than sent as an empty value the route would 422 on
  // (`status=` is not a member of the enum).
  const query = buildQueryString({
    limit: params.limit,
    offset: params.offset,
    q: params.q,
    status: params.status,
    // Sent only when true: `needs_me=false` is the default server-side, and
    // omitting it keeps the shared-link URL to the filters actually applied.
    needs_me: params.needs_me ? true : undefined,
    sort: params.sort,
  });
  return apiClient.get<FlowList>(`/orchestration/flows${query}`);
}

/**
 * Fetch execution progress and blocks for one flow (issue #5145).
 *
 * The read side of the delivery ledger: per node and cycle, the phase, whether it
 * is moving, the next scheduled check, and — when stopped — the typed block naming
 * who acts and what they supply.
 *
 * **Read-only by construction.** There is no mutating verb on this path;
 * approving a gate or resuming a node still goes through `approveGate` /
 * `resumeNode` above. The payload also carries no claim binding: the server
 * withholds `claim_id`/`claim_generation` deliberately, so nothing here can be
 * replayed as authority.
 *
 * Paged, because a long-running flow's history is unbounded. `total` reports the
 * flow's whole count so a caller showing one page can say what it is not showing,
 * and `limit` above the server ceiling is a 422 rather than a silent clamp.
 *
 * A `flow_id` belonging to another org returns the same 404 as an unknown one.
 *
 * `signal` is threaded through so a caller can abort in flight. That is not a
 * nicety on a polling read: without it, unmounting the flow page leaves requests
 * running against a screen nobody is looking at, and a response landing after
 * navigation resolves into a component that is gone.
 */
export async function getFlowExecution(
  flowId: string,
  params: FlowExecutionParams = {},
  signal?: AbortSignal
): Promise<FlowExecution> {
  const query = buildQueryString({ limit: params.limit, offset: params.offset });
  return apiClient.get<FlowExecution>(
    `/orchestration/flows/${encodeURIComponent(flowId)}/execution${query}`,
    signal
  );
}
