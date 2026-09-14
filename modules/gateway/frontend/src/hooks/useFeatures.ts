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

/** Preview subscriptions refresh independently of legacy feature snapshots. */
export const FEATURES_REVALIDATE_MS = 15_000;

/**
 * Read the same endpoint in a separate preview cache. Updating this query must
 * not update the session-long ['features'] snapshot used by current-UI routes.
 *
 * Foreground and background tabs request a refresh every 15 seconds, and focus
 * always requests one. Browsers may throttle hidden tabs; this interval is not
 * a guarantee about network completion. A completed refetch error withdraws
 * the preview until a successful response enables it again.
 */
export function useRevalidatingFeaturesQuery() {
  return useQuery({
    queryKey: ['features', 'new-ui'],
    queryFn: fetchFeatures,
    staleTime: FEATURES_REVALIDATE_MS,
    refetchInterval: FEATURES_REVALIDATE_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
    retry: 1,
  });
}

/** Initial pending reads hold deep links; completed errors override cached true. */
export function useNewUiEnabled(): { enabled: boolean; isPending: boolean } {
  const { data, isPending, isError } = useRevalidatingFeaturesQuery();
  return {
    enabled: !isPending && !isError && data?.new_ui === true,
    isPending,
  };
}
