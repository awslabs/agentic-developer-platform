/**
 * Agent Run Stats API client.
 *
 * Issue #3630/#3633: Dashboard stats for the /runs landing page.
 * Types mirror the backend StatsResponse schema
 * (modules/gateway/src/activity/stats_schemas.py) — keep in sync.
 */

import { apiClient, buildQueryString } from './api';

export interface RunStatsFailure {
  invocation_id: string;
  invoked_at: string;
  persona: string | null;
  repo: string | null;
  topic: string | null;
  error_message: string | null;
}

export interface RunStatsActiveRun {
  invocation_id: string;
  invoked_at: string;
  persona: string | null;
  repo: string | null;
  topic: string | null;
}

export interface RunStatsSpend {
  total_cost_usd: number;
  total_tokens: number;
  total_calls: number;
}

export interface RunStatsResponse {
  window_days: number;
  active_runs: RunStatsActiveRun[];
  today: {
    total: number;
    completed: number;
    failed: number;
    active: number;
    /**
     * Issue #3964: runs deliberately aborted today.
     *
     * Optional on purpose — the backend defaults it to 0, but this SPA is served
     * from CloudFront and can be a build ahead of or behind the API it talks to.
     * Typed as `number | undefined` rather than `number`, the one build that
     * matters (an older API, no field) is a `undefined` a caller must handle,
     * instead of a `0` TypeScript invented and no one can distinguish from
     * "nothing was aborted".
     *
     * No new dashboard tile ships with this field. The counter exists so an
     * aborted run stops being miscounted as failed; deciding how to *show* it is
     * a separate design question and not this story's to answer.
     */
    aborted?: number;
  };
  daily: Array<{ date: string; total: number; completed: number; failed: number; aborted?: number }>;
  by_persona: Array<{ persona: string; total: number; completed: number; failed: number; aborted?: number }>;
  recent_failures: RunStatsFailure[];
  top_repos: Array<{ repo: string; total: number }>;
  spend: RunStatsSpend | null;
}

/**
 * Fetch the current user's agent run stats.
 * @param days - Number of days to include in the stats window (default 7).
 */
export async function getRunStats(days: number = 7): Promise<RunStatsResponse> {
  const query = buildQueryString({ days });
  return apiClient.get<RunStatsResponse>(`/me/agent-run-stats${query}`);
}
