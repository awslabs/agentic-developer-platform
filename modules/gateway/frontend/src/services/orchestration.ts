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

import { apiClient } from './api';
import type { FlowGraph, GateDecisionResult, ResumeResult } from '@/types/orchestration';

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
