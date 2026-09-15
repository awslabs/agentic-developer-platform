/** Development fixtures matching overview_routes.py response models. */
import type { BudgetNode, MonthlySpend, PeopleSpend } from '@/services/budgetOverview';
const none = { amount_usd: null, source: null, source_label: null, enforcement_mode: null };
const platform = { amount_usd: '300.00', source: 'platform_default', source_label: 'Platform default', enforcement_mode: 'hard' };
const org = { amount_usd: '750.00', source: 'org_default', source_label: 'Engineering · Organization', enforcement_mode: 'hard' };
const team = { amount_usd: '500.00', source: 'team_default', source_label: 'Platform · Team', enforcement_mode: 'hard' };
export const mockBudgetHierarchy: BudgetNode = {
  key: 'platform::', kind: 'platform', name: 'Platform default', org_id: null, team_id: null, person_anchor: null,
  configured_usd: '300.00', effective: platform, fallback: none, children: [{
    key: 'org:engineering:', kind: 'org', name: 'Engineering', org_id: 'engineering', team_id: null, person_anchor: null,
    configured_usd: '750.00', effective: org, fallback: platform, children: [{
      key: 'team:engineering:platform', kind: 'team', name: 'Platform', org_id: 'engineering', team_id: 'platform', person_anchor: null,
      configured_usd: '500.00', effective: team, fallback: org, children: [{
        key: 'users:alex', kind: 'user', name: 'Alex Morgan', org_id: null, team_id: null, person_anchor: 'users:alex',
        configured_usd: null, effective: team, fallback: team, children: [],
      }],
    }],
  }],
};
export const mockMonthlySpend: MonthlySpend = {
  month: '2026-09-01', resets_at: '2026-10-01', as_of: '2026-09-14T12:00:00Z', daily_complete: true,
  totals: { direct_usd: '84.200000', cloud_usd: '163.300000', total_usd: '247.500000' },
  days: Array.from({ length: 14 }, (_, index) => ({ date: `2026-09-${String(14 - index).padStart(2, '0')}`, in_progress: index === 0,
    direct_usd: index === 0 ? '84.200000' : '0.000000', cloud_usd: index === 0 ? '163.300000' : '0.000000', total_usd: index === 0 ? '247.500000' : '0.000000' })),
};
export const mockPeopleSpend: PeopleSpend = { items: [{ person_anchor: 'users:alex', name: 'Alex Morgan', email: 'alex@example.com', spend: mockMonthlySpend.totals, budget: team }], total: 1, page: 1, page_size: 25 };
