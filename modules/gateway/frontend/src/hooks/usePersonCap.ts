/**
 * The ONE query identity for the caller's personal limit (review fix on #4686).
 *
 * The limit renders in two places — the headline denominator (`MySpend`) and the
 * editor (`PersonSpendingLimit`) — and they must be the same fetch: two hand-copied
 * `useQuery` blocks coordinated only by a matching string literal drift the first
 * time one is edited, and then a save updates the editor while the bar keeps
 * drawing the stale denominator. Every consumer imports this hook; invalidation
 * imports `personCapQueryKey` so the key exists in exactly one place.
 *
 * A separate module (not `services/personCap.ts`) so tests can mock the FETCHER
 * while this hook stays real — mocking a module cannot redirect same-module calls.
 */

import { useQuery } from '@tanstack/react-query';
import { getMyPersonCap } from '@/services/personCap';
import type { BudgetPeriodType, PersonCapResponse } from '@/types/budget';

export const personCapQueryKey = (period: BudgetPeriodType) => ['myPersonCap', period] as const;

export function usePersonCap(period: BudgetPeriodType) {
  return useQuery<PersonCapResponse>({
    queryKey: personCapQueryKey(period),
    queryFn: () => getMyPersonCap(period),
  });
}
