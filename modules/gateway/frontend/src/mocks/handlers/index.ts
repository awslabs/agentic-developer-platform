import { authHandlers } from './auth';
import { adminHandlers } from './admin';
import { indexingHandlers } from './indexing'; // Issue #1424
import { activityHandlers } from './activity'; // Issue #1457
import { poolHandlers } from './pool';
import { budgetHandlers } from './budget';
import { ratelimitHandlers } from './ratelimit';
import { logsHandlers } from './logs';
import { http, HttpResponse } from 'msw';
import { mockOrganizations, mockPoolAccounts } from '../data/organizations';

// Dashboard handlers
const dashboardHandlers = [
  http.get('/api/admin/dashboard/platform', () => {
    const healthyCount = mockPoolAccounts.filter((a) => a.is_healthy).length;
    return HttpResponse.json({
      total_organizations: mockOrganizations.length,
      total_requests_24h: 125432,
      total_tokens_24h: 45678901,
      total_cost_24h: 2345.67,
      active_users_24h: 234,
      error_rate_24h: 0.5,
      pool_status: {
        total_accounts: mockPoolAccounts.length,
        healthy_accounts: healthyCount,
        unhealthy_accounts: mockPoolAccounts.length - healthyCount,
        accounts: mockPoolAccounts,
      },
      top_organizations: mockOrganizations.map((org) => ({
        id: org.id,
        name: org.name,
        request_count: Math.floor(Math.random() * 50000),
        token_count: Math.floor(Math.random() * 10000000),
        cost_usd: Math.random() * 1000,
      })),
    });
  }),

  http.get('/api/admin/dashboard/org/:orgId', ({ params }) => {
    const org = mockOrganizations.find((o) => o.id === params.orgId);
    const now = new Date();
    const periodStart = new Date(now.getFullYear(), now.getMonth(), 1);
    const periodEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);

    return HttpResponse.json({
      org_id: params.orgId,
      org_name: org?.name || 'Unknown Organization',
      total_requests_24h: 45678,
      total_tokens_24h: 12345678,
      total_cost_24h: 567.89,
      active_users_24h: 78,
      error_rate_24h: 0.3,
      budget_status: {
        budget_amount_usd: 10000,
        current_spend_usd: 4523.45,
        remaining_budget_usd: 5476.55,
        budget_utilization_percent: 45.23,
        period_start: periodStart.toISOString(),
        period_end: periodEnd.toISOString(),
        period_type: 'monthly',
        enforcement_mode: 'soft',
        budget_exceeded: false,
        warnings: [],
      },
      top_departments: [
        { id: 'dept-001', name: 'Backend Team', request_count: 20000, token_count: 5000000, cost_usd: 200 },
        { id: 'dept-002', name: 'Frontend Team', request_count: 15000, token_count: 4000000, cost_usd: 150 },
      ],
      top_models: [
        { name: 'claude-3-sonnet', request_count: 30000, token_count: 8000000, cost_usd: 320 },
        { name: 'claude-3-haiku', request_count: 10000, token_count: 2000000, cost_usd: 40 },
        { name: 'claude-3-opus', request_count: 5000, token_count: 2000000, cost_usd: 200 },
      ],
    });
  }),

  http.get('/api/admin/metrics/system', () => {
    return HttpResponse.json({
      api_calls_per_minute: Math.floor(Math.random() * 500) + 100,
      average_latency_ms: Math.floor(Math.random() * 200) + 50,
      error_rate: Math.random() * 2,
      active_connections: Math.floor(Math.random() * 1000) + 100,
      cpu_utilization: Math.random() * 60 + 20,
      memory_utilization: Math.random() * 50 + 30,
    });
  }),
];

export const handlers = [
  ...authHandlers,
  ...adminHandlers,
  ...indexingHandlers, // Issue #1424
  ...activityHandlers, // Issue #1457
  ...poolHandlers,
  ...budgetHandlers,
  ...ratelimitHandlers,
  ...logsHandlers,
  ...dashboardHandlers,
];
