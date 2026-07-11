/**
 * API client for Agent Run Stats.
 *
 * Issue #3633: Agent Run Dashboard — stats endpoint.
 * Calls GET /me/agent-run-stats?days=N for the current user's run summary.
 */

import { apiClient, buildQueryString } from './api';

export interface RunStatsFailure {
  invocation_id: string;
  topic: string | null;
  failed_at: string;
  persona: string | null;
}

export interface RunStatsResponse {
  active_runs: Array<{ invocation_id: string; topic: string | null; persona: string | null }>;
  today: {
    total: number;
    succeeded: number;
    failed: number;
    spend: number | null;
  };
  recent_failures: RunStatsFailure[];
}

/**
 * Fetch the current user's agent run stats.
 * @param days - Number of days to include in the stats window (default 7).
 */
export async function getRunStats(days: number = 7): Promise<RunStatsResponse> {
  const query = buildQueryString({ days });
  return apiClient.get<RunStatsResponse>(`/me/agent-run-stats${query}`);
}
