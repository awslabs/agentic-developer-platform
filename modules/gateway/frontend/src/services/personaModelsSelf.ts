/**
 * Self-service persona-model client — Issue #5422 (PMM-04, design note section 6.1).
 *
 * **No function here takes a target, at any position.** Not a principal identifier,
 * not a scope object, not a destination id. The viewer is derived from the session
 * server-side, so there is nothing for the caller to pass and nothing for a component
 * to accidentally pass. A target parameter reachable from a self-service path is how
 * authority leaks; the absence of one is why it cannot.
 *
 * Administered operations live in `personaModelsAdmin.ts` and take the canonical
 * principal ID as their first argument. The page selects the correct module at the
 * scope boundary; the self-path code never imports the administered module.
 */

import { apiClient, buildQueryString } from './api';
import type {
  ManageableServicePrincipals,
  ModelCatalogue,
  PersonaCatalogue,
  PreferenceDetail,
  PreferenceList,
} from './personaModels';

const BASE = '/me/persona-models';

export function getPersonaCatalogue(signal?: AbortSignal): Promise<PersonaCatalogue> {
  return apiClient.get(`${BASE}/catalog`, signal);
}

export function getModelCatalogue(
  personaKey: string,
  signal?: AbortSignal,
): Promise<ModelCatalogue> {
  return apiClient.get(
    `${BASE}/catalog${buildQueryString({ persona_key: personaKey })}`,
    signal,
  );
}

export function getPreferences(signal?: AbortSignal): Promise<PreferenceList> {
  return apiClient.get(BASE, signal);
}

export function setPreference(
  personaKey: string,
  model: string,
  expectedRevision?: number,
): Promise<PreferenceDetail> {
  return apiClient.put(`${BASE}/${encodeURIComponent(personaKey)}`, {
    model,
    ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
  });
}

export function resetPreference(
  personaKey: string,
  expectedRevision: number,
): Promise<PreferenceDetail> {
  return apiClient.delete(`${BASE}/${encodeURIComponent(personaKey)}`, {
    expected_revision: expectedRevision,
  });
}

export function getManageableServicePrincipals(signal?: AbortSignal): Promise<ManageableServicePrincipals> {
  return apiClient.get(`${BASE}/manageable-service-principals`, signal);
}
