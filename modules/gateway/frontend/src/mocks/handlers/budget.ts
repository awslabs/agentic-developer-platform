import { http, HttpResponse } from 'msw';

const mockBudgets = [
  {
    id: 'budget-001',
    entity_type: 'org',
    entity_id: 'org-001',
    period_type: 'monthly',
    budget_amount_usd: 10000,
    enforcement_mode: 'soft',
    org_id: 'org-001',
    updated_at: new Date().toISOString(),
  },
  {
    id: 'budget-002',
    entity_type: 'department',
    entity_id: 'dept-001',
    period_type: 'monthly',
    budget_amount_usd: 5000,
    enforcement_mode: 'hard',
    org_id: 'org-001',
    updated_at: new Date().toISOString(),
  },
  {
    id: 'budget-003',
    entity_type: 'team',
    entity_id: 'team-001',
    period_type: 'monthly',
    budget_amount_usd: 1000,
    enforcement_mode: 'soft',
    org_id: 'org-001',
    updated_at: new Date().toISOString(),
  },
];

export const budgetHandlers = [
  http.get('/api/admin/organizations/:orgId/budgets', ({ params, request }) => {
    const url = new URL(request.url);
    const entityType = url.searchParams.get('entity_type');
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    let budgets = mockBudgets.filter((b) => b.org_id === params.orgId);
    if (entityType) {
      budgets = budgets.filter((b) => b.entity_type === entityType);
    }

    const start = (page - 1) * pageSize;
    const items = budgets.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: budgets.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < budgets.length,
    });
  }),

  http.get('/api/admin/organizations/:orgId/budgets/:entityType/:entityId/status', () => {
    const now = new Date();
    const periodStart = new Date(now.getFullYear(), now.getMonth(), 1);
    const periodEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);

    return HttpResponse.json({
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
    });
  }),

  http.post('/api/admin/organizations/:orgId/budgets', async ({ params, request }) => {
    const body = await request.json() as {
      entity_type: string;
      entity_id: string;
      period_type: string;
      budget_amount_usd: number;
      enforcement_mode?: string;
    };
    const newBudget = {
      id: `budget-${Date.now()}`,
      entity_type: body.entity_type,
      entity_id: body.entity_id,
      period_type: body.period_type,
      budget_amount_usd: body.budget_amount_usd,
      enforcement_mode: body.enforcement_mode || 'soft',
      org_id: params.orgId as string,
      updated_at: new Date().toISOString(),
    };
    return HttpResponse.json(newBudget, { status: 201 });
  }),

  http.patch('/api/admin/organizations/:orgId/budgets/:budgetId', async ({ params, request }) => {
    const budget = mockBudgets.find((b) => b.id === params.budgetId);
    if (!budget) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Budget not found' },
        { status: 404 }
      );
    }
    const body = await request.json() as Record<string, unknown>;
    return HttpResponse.json({ ...budget, ...body, updated_at: new Date().toISOString() });
  }),

  // Update budget by entity type and entity ID (Issue #220)
  http.put('/api/admin/organizations/:orgId/budget/:entityType/:entityId', async ({ params, request }) => {
    const budget = mockBudgets.find(
      (b) => b.entity_type === params.entityType && b.entity_id === params.entityId
    );
    const body = await request.json() as { budget_amount_usd?: number; enforcement_mode?: string };

    const updatedBudget = budget
      ? { ...budget, ...body, updated_at: new Date().toISOString() }
      : {
          org_id: params.orgId as string,
          entity_type: params.entityType as string,
          entity_id: params.entityId as string,
          period_type: 'monthly',
          budget_amount_usd: body.budget_amount_usd || 0,
          enforcement_mode: body.enforcement_mode || 'soft',
          updated_at: new Date().toISOString(),
        };

    return HttpResponse.json(updatedBudget);
  }),

  http.delete('/api/admin/organizations/:orgId/budgets/:budgetId', () => {
    return HttpResponse.json({ success: true });
  }),

  // Delete budget by entity type, entity ID, and period type (Issue #220)
  http.delete('/api/admin/organizations/:orgId/budget/:entityType/:entityId/:periodType', () => {
    return HttpResponse.json({ success: true });
  }),

  http.get('/api/admin/organizations/:orgId/usage', ({ request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    const usageRecords = Array.from({ length: 10 }, (_, i) => ({
      id: `usage-${i + 1}`,
      entity_type: 'org',
      entity_id: 'org-001',
      period_start: new Date(Date.now() - i * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
      period_type: 'daily',
      total_cost_usd: Math.random() * 500,
      total_tokens: Math.floor(Math.random() * 1000000),
      request_count: Math.floor(Math.random() * 10000),
      org_id: 'org-001',
    }));

    const start = (page - 1) * pageSize;
    const items = usageRecords.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: usageRecords.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < usageRecords.length,
    });
  }),

  http.get('/api/admin/organizations/:orgId/usage/timeseries', () => {
    const data = Array.from({ length: 7 }, (_, i) => ({
      timestamp: new Date(Date.now() - (6 - i) * 24 * 60 * 60 * 1000).toISOString(),
      request_count: Math.floor(Math.random() * 10000) + 1000,
      token_count: Math.floor(Math.random() * 1000000) + 100000,
      cost_usd: Math.random() * 500 + 50,
      error_count: Math.floor(Math.random() * 100),
    }));
    return HttpResponse.json(data);
  }),
];
