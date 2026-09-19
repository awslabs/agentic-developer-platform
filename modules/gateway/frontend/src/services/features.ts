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
  /** Opt-in /next UI shell — Issue #5079. Fail-closed; rollout control only. */
  new_ui: boolean;
  /** Superplane domain app — Issue #5037 (EPIC #4910). Fail-closed. */
  superplane: boolean;
  /** Per-persona model preferences — Issue #5422. Fail-closed until PMM-09. */
  agent_models: boolean;
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
  // Fail-closed — Issue #5079. The current UI is the default; /next is an opt-in
  // additional experience. `useFeatures` returns `data ?? ALL_FEATURES_ENABLED`, so
  // this value renders BOTH while /features is in flight AND whenever it errors. A
  // `true` here would advertise "Try the new UI" on every cold load before the flags
  // arrive, and keep advertising it during any backend outage — and it would defeat
  // the rollback, which is "flip the flag off and the new shell is gone".
  new_ui: false,
  // Fail-closed — Issue #5037. `useFeatures` returns `data ?? ALL_FEATURES_ENABLED`, so
  // this value renders BOTH while the /features fetch is in flight AND whenever it
  // errors. A `true` here would surface a Superplane nav entry and route on every cold
  // load before the flags arrive, and keep surfacing them during any backend outage.
  //
  // It would also break the story's first acceptance criterion in the most direct way
  // available: the criterion is that no existing ADP surface changes behaviour while the
  // gate is off, and a fail-open default means the gate is never observably off.
  superplane: false,
  // Fail-closed — Issue #5422. The screen is useful only after catalogue
  // evidence and enforcement readiness, and the flag is its rollback lever.
  agent_models: false,
};

export async function fetchFeatures(): Promise<FeatureFlags> {
  const response = await apiClient.get<FeaturesResponse>('/features');
  return response.features;
}
