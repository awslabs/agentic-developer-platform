import { apiClient } from './api';

export interface BudgetBalance {
  limit_usd: string | null;
  spent_usd: string | null;
  reserved_usd: string | null;
  remaining_usd: string | null;
  /** Parts of reserved_usd; absent when an older server cannot classify holds. */
  request_reserved_usd?: string | null;
  recovery_hold_usd?: string | null;
}

export interface ProposedBudget {
  limit_usd: string | null;
  concurrency: number | null;
  attempts_per_node: number | null;
  wall_clock_seconds: number | null;
  expires_at: string | null;
  plan_version: number;
  plan_hash: string;
}

export interface FlowBudget {
  flow_id: string;
  observed_at: string;
  balance: BudgetBalance;
  run_limit_usd: string | null;
  concurrency: number | null;
  enforcement_enabled: boolean;
  accounting_incomplete: boolean;
  /** `accepted`: balance.limit_usd is in force. `proposed`: nothing is in force; `proposed` shows what acceptance grants. */
  policy_status: 'accepted' | 'proposed' | 'none';
  proposed: ProposedBudget | null;
}

export interface NodeBudget {
  flow_id: string;
  node_id: string;
  observed_at: string;
  enforcement_enabled: boolean;
  runs: { run_id: string; persona: string | null; last_call_at: string; balance: BudgetBalance }[];
  truncated: boolean;
}

export const getFlowBudget = (flowId: string, signal?: AbortSignal) =>
  apiClient.get<FlowBudget>(`/orchestration/flows/${encodeURIComponent(flowId)}/budget`, signal);

export const getNodeBudget = (flowId: string, nodeId: string, signal?: AbortSignal) =>
  apiClient.get<NodeBudget>(`/orchestration/flows/${encodeURIComponent(flowId)}/nodes/${encodeURIComponent(nodeId)}/budget`, signal);
