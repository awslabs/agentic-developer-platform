/**
 * MSW handlers for Agent Run Stats endpoint.
 *
 * Issue #3633: Agent Run Dashboard — mock data for development.
 * Shape mirrors the backend StatsResponse schema
 * (modules/gateway/src/activity/stats_schemas.py).
 */

import { http, HttpResponse } from 'msw';

export const runStatsHandlers = [
  http.get('/api/me/agent-run-stats', () => {
    return HttpResponse.json({
      window_days: 7,
      active_runs: [
        {
          invocation_id: 'inv-active-001',
          invoked_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
          persona: 'developer',
          repo: 'acme/api',
          topic: 'Deploy gateway service',
        },
        {
          invocation_id: 'inv-active-002',
          invoked_at: new Date(Date.now() - 25 * 60 * 1000).toISOString(),
          persona: 'developer',
          repo: 'acme/api',
          topic: 'Fix authentication bug',
        },
      ],
      today: {
        total: 12,
        completed: 8,
        failed: 2,
        active: 2,
      },
      daily: [],
      by_persona: [],
      recent_failures: [
        {
          invocation_id: 'inv-fail-001',
          invoked_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
          persona: 'developer',
          repo: 'acme/api',
          topic: 'Upgrade dependencies',
          error_message: null,
        },
        {
          invocation_id: 'inv-fail-002',
          invoked_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
          persona: 'architect',
          repo: 'acme/web',
          topic: 'Refactor auth module',
          error_message: null,
        },
      ],
      top_repos: [],
      spend: { total_cost_usd: 4.56, total_tokens: 120000, total_calls: 42 },
    });
  }),
];
