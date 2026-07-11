/**
 * MSW handlers for Agent Run Stats endpoint.
 *
 * Issue #3633: Agent Run Dashboard — mock data for development.
 */

import { http, HttpResponse } from 'msw';

export const runStatsHandlers = [
  http.get('/api/me/agent-run-stats', () => {
    return HttpResponse.json({
      active_runs: [
        {
          invocation_id: 'inv-active-001',
          topic: 'Deploy gateway service',
          persona: 'developer',
        },
        {
          invocation_id: 'inv-active-002',
          topic: 'Fix authentication bug',
          persona: 'developer',
        },
      ],
      today: {
        total: 12,
        succeeded: 8,
        failed: 2,
        spend: 4.56,
      },
      recent_failures: [
        {
          invocation_id: 'inv-fail-001',
          topic: 'Upgrade dependencies',
          failed_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
          persona: 'developer',
        },
        {
          invocation_id: 'inv-fail-002',
          topic: 'Refactor auth module',
          failed_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
          persona: 'architect',
        },
      ],
    });
  }),
];
