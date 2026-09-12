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
  /** Budget & Spend screen — Issue #4402. Fail-closed while the EPIC lands. */
  budget_spend: boolean;
  /** Live run controls (pause/resume/steer/abort) — Issue #3960. Fail-closed. */
  agent_control: boolean;
}

export interface FeaturesResponse {
  features: FeatureFlags;
}

/** All features enabled — used as fail-open default.
 *  Exceptions: gitlab (fail-closed, Issue #3773), orchestration_engine
 *  (fail-closed, Issue #4209) and budget_spend (fail-closed, Issue #4402)
 *  default to false. The engine + graph UI are a per-flow opt-in add-on;
 *  legacy GitHub-driven mode stays the default, so a pending or failed
 *  /features fetch must NOT reveal the new path. */
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
  // Fail-closed: the rollback plan for #4402 is "flip the flag off", which only
  // works if a pending or failed /features fetch also resolves to off. A fail-open
  // default would make the screen reappear during exactly the outage it was
  // switched off for.
  budget_spend: false,
  // Fail-closed — Issue #3960, and the most consequential `false` in this object.
  // `useFeatures` returns `data ?? ALL_FEATURES_ENABLED`, so this value is what
  // renders BOTH while the /features fetch is in flight AND whenever it errors. A
  // `true` here would show pause/steer/abort controls on every page load before
  // the flags arrive, and keep showing them during any backend outage — exactly
  // when the controls cannot work. An operator who clicks Abort during an outage
  // and sees no error has been told a run was aborted when it was not (AC-F3).
  agent_control: false,
};

export async function fetchFeatures(): Promise<FeatureFlags> {
  const response = await apiClient.get<FeaturesResponse>('/features');
  return response.features;
}
