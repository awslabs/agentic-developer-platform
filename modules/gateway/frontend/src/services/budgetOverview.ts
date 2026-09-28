import { apiClient } from './api';

export interface SpendAmounts { direct_usd: string; cloud_usd: string; total_usd: string }
export interface MonthlySpend {
  month: string; resets_at: string; as_of: string; totals: SpendAmounts; daily_complete: boolean;
  days: (SpendAmounts & { date: string; in_progress: boolean })[];
}
export interface BudgetLimit { amount_usd: string | null; source: string | null; source_label: string | null; enforcement_mode: string | null }
export interface BudgetNode {
  key: string; kind: 'platform' | 'org' | 'team' | 'user'; name: string;
  org_id: string | null; team_id: string | null; person_anchor: string | null;
  configured_usd: string | null; effective: BudgetLimit; fallback: BudgetLimit; children: BudgetNode[];
}
export interface PeopleSpend {
  items: { person_anchor: string; name: string; email: string; spend: SpendAmounts; budget: BudgetLimit }[];
  total: number; page: number; page_size: number;
}
export const getMonthlySpend = () => apiClient.get<MonthlySpend>('/me/budget/monthly-spend');
export const getBudgetHierarchy = () => apiClient.get<BudgetNode>('/budget/hierarchy');
export const getPeopleSpend = (page: number, search: string) => apiClient.get<PeopleSpend>(`/budget/people-spend?page=${page}&search=${encodeURIComponent(search)}`);
