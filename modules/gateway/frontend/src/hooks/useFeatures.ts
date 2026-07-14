/**
 * useFeatures hook — Issue #3566.
 *
 * Fetches deployment-level feature flags via GET /api/features.
 * Fail-open: if the fetch fails or is pending, all features are treated as enabled.
 * Uses staleTime: Infinity so the flags are fetched once per session.
 */

import { useQuery } from '@tanstack/react-query';
import { fetchFeatures, ALL_FEATURES_ENABLED } from '@/services/features';
import type { FeatureFlags } from '@/services/features';

export function useFeatures(): FeatureFlags {
  const { data } = useQuery({
    queryKey: ['features'],
    queryFn: fetchFeatures,
    staleTime: Infinity,
    retry: 1,
  });

  // Fail-open: return all-enabled when data is unavailable (loading or error)
  return data ?? ALL_FEATURES_ENABLED;
}
