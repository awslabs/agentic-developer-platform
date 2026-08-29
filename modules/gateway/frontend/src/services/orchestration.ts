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
import type { FlowGraph } from '@/types/orchestration';

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
