/**
 * Shared types and utilities for Agent Models (PMM-04, issue #5422).
 *
 * Self-service operations: `personaModelsSelf.ts` (no target parameter).
 * Administered operations: `personaModelsAdmin.ts` (target as first argument).
 *
 * This module holds only the wire-shape types, the conflict type guard, and the
 * error-message extractor shared by both paths. Design note section 6.1 explains
 * why the split is structural: the absence of a target parameter on the self path
 * is the security property; a shared scope helper would undermine it.
 */

export type PreferenceSource = 'principal-mapping' | 'system-default';

/**
 * Proof state of a compatibility class's default model, as decided by the server.
 *
 * `proven` means the class default crossed the real-harness invocation gate.
 * `candidate` means a default is proposed but has never been proven.
 * `null` means the class has NO default recorded at all — an actionable platform
 * readiness gap (approved design, decision 4), never a fallback to another class.
 *
 * The page must render this field rather than deriving proof from
 * `effective_is_candidate`: that boolean is false both for a proven default and
 * for "no default exists", so deriving from it reports an absent default as proven.
 */
export type ClassDefaultStatus = 'candidate' | 'proven' | null;

export interface PersonaPreference {
  default_model_id?: string | null;
  default_scope?: 'persona' | 'class';
  persona_key: string;
  persona_display_name: string;
  configurable: boolean;
  compatibility_class: string;
  harness_contract_revision: string;
  model_lifecycle?: string | null;
  availability_status?: string;
  availability_reason?: string | null;
  warnings?: string[];
  effective_model_id: string | null;
  effective_is_candidate: boolean;
  source: PreferenceSource;
  status: 'configured' | 'not-configured' | 'unavailable' | 'disallowed' | 'stale';
  /** Server-owned proof state of the class default; null when none is recorded. */
  class_default_status?: ClassDefaultStatus;
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
  default_scope?: 'persona' | 'class';
  persona_key: string;
  compatibility_class: string;
  harness_contract_revision: string;
  model_lifecycle?: string | null;
  availability_status?: string;
  availability_reason?: string | null;
  warnings?: string[];
  effective_model_id: string | null;
  effective_is_candidate: boolean;
  source: PreferenceSource;
  status: string;
  /** Server-owned proof state of the class default; null when none is recorded. */
  class_default_status?: ClassDefaultStatus;
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
  aliases: string[];
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
  canonical_service_principal_id: string;
  principal_kind: 'service_account';
  display_name: string;
  tenant_label: string;
  source: string;
  manageable: boolean;
}

export interface ManageableServicePrincipals {
  principals: ManageableServicePrincipal[];
}

export interface PersonaModelConflict {
  persona_key: string;
  current_model_id: string;
  current_revision: number;
  effective_model_id: string;
  default_model_id: string | null;
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

/** Translate known refusal codes; keep backend diagnostics out of the settings page. */
export function personaModelErrorMessage(error: unknown, fallback: string): string {
  const value = error as { reason?: unknown; error?: unknown; detail?: unknown } | null;
  const detail = value?.detail;
  const structured = detail && typeof detail === 'object'
    ? detail as { reason?: unknown; error?: unknown } : value;
  const reason = structured?.reason ?? structured?.error;
  const messages: Record<string, string> = {
    default_revision_conflict: 'This default changed elsewhere. Reload defaults before saving again.',
    model_unproven: 'This model needs an availability check before it can become a platform default.',
    platform_destination_unavailable: 'The platform model connection is not ready. Configure it before saving defaults.',
    platform_account_unconfigured: 'Configure the platform model account before saving defaults.',
    unknown_model: 'This model is no longer listed. Reload the model choices.',
    not_invocable: 'This model is currently unavailable. Choose another model.',
    evidence_stale: 'Model availability needs to be checked. Try another available model or contact your ADP administrator.',
    probing_disabled: 'Model choices are not ready yet. Contact your ADP administrator.',
    not_yet_certified: 'This model is not ready to use yet. Choose another model.',
    retired: 'This model has been retired. Choose another model.',
    not_permitted: 'Your organization does not allow this model. Choose another model or contact your ADP administrator.',
    harness_incompatible: 'This persona does not support this model.',
    persona_not_configurable: 'Model selection is not available for this persona.',
    access_denied: 'You do not have permission to change these settings.',
    cross_tenant_denied: 'You do not have permission to change these settings.',
  };
  return typeof reason === 'string' && Object.prototype.hasOwnProperty.call(messages, reason) ? messages[reason] : fallback;
}
