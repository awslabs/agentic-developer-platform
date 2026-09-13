/**
 * useFeatures hook — Issue #3566.
 *
 * Fetches deployment-level feature flags via GET /api/features.
 * Applies each feature's configured default while the fetch is pending or fails.
 * Uses staleTime: Infinity so the flags are fetched once per session.
 */

import { useQuery } from '@tanstack/react-query';
import { fetchFeatures, ALL_FEATURES_ENABLED } from '@/services/features';
import type { FeatureFlags } from '@/services/features';

export function useFeaturesQuery() {
  return useQuery({
    queryKey: ['features'],
    queryFn: fetchFeatures,
    staleTime: Infinity,
    retry: 1,
  });
}

export function useFeatures(): FeatureFlags {
  const { data } = useFeaturesQuery();
  // Defaults retain each feature's existing policy, including fail-closed flags.
  return data ?? ALL_FEATURES_ENABLED;
}
