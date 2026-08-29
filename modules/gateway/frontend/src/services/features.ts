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
  logs: boolean;
  gitlab: boolean;
  orchestration_engine: boolean;
}

export interface FeaturesResponse {
  features: FeatureFlags;
}

/** All features enabled — used as fail-open default.
 *  Exceptions: gitlab (fail-closed, Issue #3773) and orchestration_engine
 *  (fail-closed, Issue #4209) default to false. The engine + graph UI are a
 *  per-flow opt-in add-on; legacy GitHub-driven mode stays the default, so a
 *  pending or failed /features fetch must NOT reveal the new path. */
export const ALL_FEATURES_ENABLED: FeatureFlags = {
  chat: true,
  knowledge: true,
  indexing: true,
  connections: true,
  credentials: true,
  system_dashboard: true,
  logs: true,
  gitlab: false,
  orchestration_engine: false,
};

export async function fetchFeatures(): Promise<FeatureFlags> {
  const response = await apiClient.get<FeaturesResponse>('/features');
  return response.features;
}
