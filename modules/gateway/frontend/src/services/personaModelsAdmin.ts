/**
 * Administered persona-model client — Issue #5422 (PMM-04, design note section 6.1).
 *
 * **Every function here takes the canonical service principal ID as its first argument.**
 * These are the counterparts of the target-free functions in `personaModelsSelf.ts`,
 * routed to `/service-principals/{id}/persona-models/...` instead of `/me/persona-models`.
 *
 * The canonical principal ID is an opaque, server-returned value from the manageable
 * principals list. The page must never construct or accept an arbitrary identifier.
 */

import { apiClient, buildQueryString } from './api';
import type {
  ModelCatalogue,
  PreferenceDetail,
  PreferenceList,
} from './personaModels';

function adminBase(canonicalPrincipalId: string): string {
  return `/service-principals/${encodeURIComponent(canonicalPrincipalId)}/persona-models`;
}

export function getPreferences(
  canonicalPrincipalId: string,
  signal?: AbortSignal,
): Promise<PreferenceList> {
  return apiClient.get(adminBase(canonicalPrincipalId), signal);
}

export function getModelCatalogue(
  canonicalPrincipalId: string,
  personaKey: string,
  signal?: AbortSignal,
): Promise<ModelCatalogue> {
  return apiClient.get(
    `${adminBase(canonicalPrincipalId)}/catalog${buildQueryString({ persona_key: personaKey })}`,
    signal,
  );
}

export function setPreference(
  canonicalPrincipalId: string,
  personaKey: string,
  model: string,
  expectedRevision?: number,
): Promise<PreferenceDetail> {
  return apiClient.put(`${adminBase(canonicalPrincipalId)}/${encodeURIComponent(personaKey)}`, {
    model,
    ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
  });
}

export function resetPreference(
  canonicalPrincipalId: string,
  personaKey: string,
  expectedRevision: number,
): Promise<PreferenceDetail> {
  return apiClient.delete(`${adminBase(canonicalPrincipalId)}/${encodeURIComponent(personaKey)}`, {
    expected_revision: expectedRevision,
  });
}
