/** Typed client for Agent Models (PMM-04, issue #5422). */

import { apiClient, buildQueryString } from './api';

export type PreferenceSource = 'principal-mapping' | 'system-default';

export interface PersonaPreference {
  persona_key: string;
  persona_display_name: string;
  configurable: boolean;
  effective_model_id: string | null;
  source: PreferenceSource;
  status: 'configured' | 'not-configured' | 'unavailable' | 'disallowed' | 'stale';
  saved_model_id: string | null;
  requested_alias: string | null;
  revision: number | null;
  updated_at: string | null;
}

export interface PreferenceList {
  principal_kind: string;
  principal_id: string;
  entries: PersonaPreference[];
}

export interface PreferenceDetail {
  persona_key: string;
  effective_model_id: string | null;
  source: PreferenceSource;
  status: string;
  saved_model_id: string | null;
  requested_alias: string | null;
  revision: number | null;
  updated_at: string | null;
  default_model_id: string | null;
  default_source: string;
}

export interface PersonaCatalogueRow {
  key: string;
  display_name: string;
  purpose: string;
  configurable: boolean;
  not_configurable_reason: string | null;
  compatibility_class: string;
}

export interface PersonaCatalogue {
  personas: PersonaCatalogueRow[];
}

export interface InvocabilityEvidence {
  account_id: string;
  region: string;
  verified_at: string;
  expires_at: string;
  stale: boolean;
  request_shape_sha256?: string | null;
}

export interface PriceContext {
  input_per_million_tokens: number | null;
  output_per_million_tokens: number | null;
}

export interface ModelCatalogueRow {
  canonical_model_id: string;
  model_family: string;
  canonical_version: string;
  selectable: boolean;
  reason: string | null;
  permitted: boolean | null;
  invocable: boolean | null;
  evidence: InvocabilityEvidence | null;
  compatibility_class: string;
  harness_contract_revision: string;
  retired: boolean;
  price_context: PriceContext | null;
}

export interface ModelCatalogue {
  persona_key: string;
  compatibility_class: string;
  models: ModelCatalogueRow[];
}

export interface ManageableServicePrincipal {
  canonical_principal_id: string;
  principal_kind: 'service_account';
  display_name: string;
  tenant_label: string;
  source: string;
  manageable: boolean;
}

export interface ManageableServicePrincipals {
  principals: ManageableServicePrincipal[];
}

export interface PersonaModelScope {
  kind: 'self' | 'service';
  canonicalPrincipalId?: string;
}

export interface PersonaModelConflict {
  persona_key: string;
  current_model_id: string;
  current_revision: number;
  effective_model_id: string;
  default_model_id: string | null;
}

function scopeBase(scope: PersonaModelScope): string {
  if (scope.kind === 'self') return '/me/persona-models';
  if (!scope.canonicalPrincipalId) throw new Error('Managed scope requires a canonical principal ID.');
  return `/service-principals/${encodeURIComponent(scope.canonicalPrincipalId)}/persona-models`;
}

export function getPersonaCatalogue(signal?: AbortSignal): Promise<PersonaCatalogue> {
  return apiClient.get('/me/persona-models/catalog', signal);
}

export function getModelCatalogue(personaKey: string, signal?: AbortSignal): Promise<ModelCatalogue> {
  return apiClient.get(
    `/me/persona-models/catalog${buildQueryString({ persona_key: personaKey })}`,
    signal,
  );
}

export function getPreferences(scope: PersonaModelScope, signal?: AbortSignal): Promise<PreferenceList> {
  return apiClient.get(scopeBase(scope), signal);
}

export function getManageableServicePrincipals(signal?: AbortSignal): Promise<ManageableServicePrincipals> {
  return apiClient.get('/me/persona-models/manageable-service-principals', signal);
}

export function setPreference(
  scope: PersonaModelScope,
  personaKey: string,
  model: string,
  expectedRevision?: number,
): Promise<PreferenceDetail> {
  return apiClient.put(`${scopeBase(scope)}/${encodeURIComponent(personaKey)}`, {
    model,
    ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
  });
}

export function resetPreference(scope: PersonaModelScope, personaKey: string): Promise<PreferenceDetail> {
  // PMM-02's merged DELETE route currently accepts no request body.
  return apiClient.delete(`${scopeBase(scope)}/${encodeURIComponent(personaKey)}`);
}

export function isPersonaModelConflict(error: unknown): error is PersonaModelConflict {
  const value = error as Partial<PersonaModelConflict> | null;
  return Boolean(
    value &&
      typeof value.persona_key === 'string' &&
      typeof value.current_model_id === 'string' &&
      typeof value.current_revision === 'number',
  );
}

export function personaModelErrorMessage(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown })?.detail;
  if (typeof detail === 'string' && detail) return detail;
  const structured = detail as { message?: string } | undefined;
  if (structured?.message) return structured.message;
  return (error as { message?: string })?.message || fallback;
}
