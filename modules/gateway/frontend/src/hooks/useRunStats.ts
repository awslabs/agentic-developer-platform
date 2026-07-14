/**
 * React Query hook for Agent Run Stats.
 *
 * Issue #3633: Agent Run Dashboard — data fetching hook.
 * Polls every 60 seconds to match backend cache TTL.
 */

import { useQuery } from '@tanstack/react-query';
import { getRunStats } from '@/services/runStats';
import type { RunStatsResponse } from '@/services/runStats';

export function useRunStats(days: number = 7) {
  return useQuery<RunStatsResponse>({
    queryKey: ['runStats', days],
    queryFn: () => getRunStats(days),
    refetchInterval: 60_000,
  });
}
