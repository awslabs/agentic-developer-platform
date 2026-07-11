/**
 * Feature flags API client — Issue #3566.
 *
 * Fetches deployment-level feature gates from GET /api/features.
 */

import { apiClient } from './api';

export interface FeatureFlags {
  chat: boolean;
  knowledge: boolean;
  indexing: boolean;
  connections: boolean;
  credentials: boolean;
  system_dashboard: boolean;
}

export interface FeaturesResponse {
  features: FeatureFlags;
}

/** All features enabled — used as fail-open default. */
export const ALL_FEATURES_ENABLED: FeatureFlags = {
  chat: true,
  knowledge: true,
  indexing: true,
  connections: true,
  credentials: true,
  system_dashboard: true,
};

export async function fetchFeatures(): Promise<FeatureFlags> {
  const response = await apiClient.get<FeaturesResponse>('/features');
  return response.features;
}
