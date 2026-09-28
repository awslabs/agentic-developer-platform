import { http, HttpResponse } from 'msw';
import { mockBudgetHierarchy, mockMonthlySpend, mockPeopleSpend } from '../data/budgetOverview';

export const budgetOverviewHandlers = [
  http.get('/api/me/budget/monthly-spend', () => HttpResponse.json(mockMonthlySpend)),
  http.get('/api/budget/hierarchy', () => HttpResponse.json(mockBudgetHierarchy)),
  http.get('/api/budget/people-spend', ({ request }) => {
    const params = new URL(request.url).searchParams;
    const page = Number(params.get('page') || 1);
    const search = (params.get('search') || '').toLowerCase();
    const items = mockPeopleSpend.items.filter(person => `${person.name} ${person.email}`.toLowerCase().includes(search));
    return HttpResponse.json({ ...mockPeopleSpend, items: page === 1 ? items : [], total: items.length, page });
  }),
  http.get('/api/me/bedrock-routing/selection', () => HttpResponse.json({
    effective: { user_id: 'alex', rung: 'team', account_id: '123456789012', destination_label: 'Development',
      destination_id: 'development', source: 'platform_admin', overrides_self_selection: false, shadowed_rung: 'platform', shadowed_account_id: null },
    own_selection: null, own_selection_active: false, selectable_connections: [],
  })),
];
