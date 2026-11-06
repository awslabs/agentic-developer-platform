import { apiClient } from './api';
import type { RecordReference } from './products';
import type { Versioned } from './l5Services';

export type ConsentService = 'security' | 'devops' | 'finops' | 'well_architected' | 'transform';
export const consentWorkers: Record<ConsentService, string> = {
  security: 'l5-worker', devops: 'l5-devops-worker', finops: 'l5-finops-worker',
  well_architected: 'l5-well-architected-worker', transform: 'l5-transform-worker',
};
export interface ConsentConfiguration {
  configuration_id: string; revision: number; hash: string;
  scope: { tenant_id: string; product_id: string; project_id: string; owner_id?: string };
  settings: { enabled: boolean; capabilities?: string[]; triggers?: unknown[]; devops?: Record<string, unknown> };
}
export interface ConsentBinding {
  binding_id: string; configuration_id: string; generation: number; state: string;
  scope: { tenant_id: string; product_id: string; project_id: string };
}
export interface ConsentScope {
  tenant_id: string; product_id: string; project_id: string; service_id: string;
  configuration_id: string; configuration_revision: number; configuration_hash: string;
  created_by: string; credential_id: string; provider_account_id: string; provider_role_arn: string; region: string;
  binding_id: string | null; binding_generation: number;
  operations: string[]; capabilities: string[]; sources: RecordReference[]; visibility: string;
  [key: string]: unknown;
}
export interface ConsentPreview { scope: ConsentScope; scope_digest: string; maximum_lifetime_seconds: number }
export interface OwnerGrant {
  delegation_id: string; revision: number; tenant_id: string; product_id: string; project_id: string; service_id: string;
  credential_id: string; binding_generation: number; operations: string[]; capabilities: string[];
  sources: RecordReference[]; visibility: string; execution_scope: ConsentScope | null;
  expires_at: string; revoked_at: string | null; active: boolean;
}
export interface ConsentSelection { binding_id?: string; for_control: boolean; control_action?: 'devops_setup' }
export interface ConsentRequest extends ConsentSelection { scope_digest: string; expires_at: string }
export interface ConsentReceipt {
  key: string; etag: string; body: ConsentRequest; scope: ConsentScope;
  state: 'pending' | 'confirmed' | 'rejected' | 'expired'; delegation_id?: string;
}
export const consentPath = (productId: string, projectId: string, service: ConsentService) =>
  `/v1/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}/services/${service}`;
export const listOwnerGrants = (productId: string, projectId: string, service: ConsentService) =>
  apiClient.get<{ items: OwnerGrant[] }>(`${consentPath(productId, projectId, service)}/delegations`);
export const listConsentBindings = (productId: string, projectId: string, service: ConsentService) =>
  apiClient.get<{ items: ConsentBinding[] }>(`${consentPath(productId, projectId, service)}/bindings`);
export const previewOwnerConsent = (productId: string, projectId: string, service: ConsentService, selection: ConsentSelection) => {
  const query = new URLSearchParams({ for_control: String(selection.for_control) });
  if (selection.binding_id) query.set('binding_id', selection.binding_id);
  if (selection.control_action) query.set('control_action', selection.control_action);
  return apiClient.requestWithEtag<ConsentPreview>(`${consentPath(productId, projectId, service)}/delegation-scope?${query}`);
};
// Callers retain this complete request before sending it. A retry never creates a new key or expiry.
export const grantOwnerConsent = (productId: string, projectId: string, service: ConsentService, receipt: ConsentReceipt) =>
  apiClient.requestWithEtag<OwnerGrant>(`${consentPath(productId, projectId, service)}/delegations`, {
    method: 'POST', headers: { 'If-Match': receipt.etag, 'Idempotency-Key': receipt.key }, body: receipt.body,
  });
export const revokeOwnerConsent = (productId: string, projectId: string, service: ConsentService, grant: OwnerGrant) =>
  apiClient.requestWithEtag<OwnerGrant>(`${consentPath(productId, projectId, service)}/delegations/${encodeURIComponent(grant.delegation_id)}`, {
    method: 'DELETE', headers: { 'If-Match': `"${grant.revision}"` },
  });

const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`;
  return JSON.stringify(value);
};
export const sameConsentScope = (left: unknown, right: unknown) => canonical(left) === canonical(right);
export const expiryValid = (expiry: string, now = Date.now()) => /^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?Z$/.test(expiry)
  && Date.parse(expiry) > now + 60_000 && Date.parse(expiry) <= now + 86_400_000;
export const matchesConsentReceipt = (grant: OwnerGrant, receipt: ConsentReceipt) =>
  sameConsentScope(grant.execution_scope, receipt.scope) && Date.parse(grant.expires_at) === Date.parse(receipt.body.expires_at);
export const consentScopeMatches = (scope: { tenant_id: string; product_id: string; project_id: string }, configuration: ConsentConfiguration) =>
  scope?.tenant_id === configuration.scope.tenant_id && scope.product_id === configuration.scope.product_id
  && scope.project_id === configuration.scope.project_id;
export type DevOpsSetupTarget = { action: 'create'; native_resource_ref?: never }
  | { action: 'link'; native_resource_ref: RecordReference };
// Build only from the saved selection; an adopted space is pinned to its hosting
// connection and its exact ID, never a user-entered replacement during dispatch.
export function devOpsSetupTarget(settings: ConsentConfiguration['settings'], scope?: ConsentConfiguration['scope']): DevOpsSetupTarget | null {
  const selected = settings.devops;
  if (selected?.space_mode === 'create' && !selected.existing_space_id) return { action: 'create' };
  if (selected?.space_mode !== 'adopt' || typeof selected.existing_space_id !== 'string'
    || !/^[A-Za-z0-9_-]{1,128}$/.test(selected.existing_space_id)) return null;
  const connection = selected.hosting_connection_ref as RecordReference | undefined;
  if (!connection || !sameConsentScope(Object.keys(connection).sort(),
    ['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'].sort())
    || !['tenant_id', 'project_id', 'source_connection_id', 'record_id', 'record_revision'].every(field =>
    typeof connection[field as keyof RecordReference] === 'string' && connection[field as keyof RecordReference].trim().length > 0)
    || connection.record_type !== 'connection' || connection.record_id !== connection.source_connection_id
    || (scope && (connection.tenant_id !== scope.tenant_id || connection.project_id !== scope.project_id))) return null;
  return { action: 'link', native_resource_ref: { ...connection, record_type: 'agent_space',
    record_id: selected.existing_space_id, record_revision: selected.existing_space_id } };
}
export function devOpsSetupDraft(settings: ConsentConfiguration['settings'], scope?: ConsentConfiguration['scope']) {
  const selected = settings.devops;
  const empty = (value: unknown) => value === undefined || value === null || (Array.isArray(value) && value.length === 0);
  return settings.enabled === false && settings.capabilities?.length === 1 && settings.capabilities[0] === 'investigation'
    && empty(settings.triggers) && selected !== undefined && devOpsSetupTarget(settings, scope) !== null
    && empty(selected.integrations) && empty(selected.trigger_policies) && empty(selected.incident_sources)
    && !selected.prevention_enabled && !(selected.prevention_policy as { enabled?: boolean } | undefined)?.enabled
    && (!selected.browser_launch_policy || selected.browser_launch_policy === 'disabled') && empty(selected.permitted_native_actions);
}
export function validConsentPreview(result: Versioned<ConsentPreview>, configuration: ConsentConfiguration,
  service: ConsentService, selection: ConsentSelection, binding?: ConsentBinding) {
  const scope = result.value?.scope;
  const target = devOpsSetupTarget(configuration.settings, configuration.scope);
  const setup = service === 'devops' && selection.control_action === 'devops_setup' && selection.for_control
    && !selection.binding_id && devOpsSetupDraft(configuration.settings, configuration.scope);
  return Boolean(scope && consentScopeMatches(scope, configuration) && scope.service_id === consentWorkers[service]
    && (!selection.control_action || setup)
    && Array.isArray(scope.capabilities) && Array.isArray(scope.sources)
    && scope.configuration_id === configuration.configuration_id && scope.configuration_revision === configuration.revision
    && scope.configuration_hash === configuration.hash && result.etag === `"${configuration.hash}"`
    && typeof configuration.scope.owner_id === 'string' && configuration.scope.owner_id.trim().length > 0
    && scope.created_by === configuration.scope.owner_id
    && typeof scope.created_by === 'string' && scope.created_by.length > 0
    && scope.binding_id === (selection.binding_id ?? null)
    && scope.binding_generation === (binding ? binding.generation + Number(selection.for_control) : 1)
    && (setup ? scope.control_action === 'devops_setup' && scope.purpose === 'devops_setup'
      && sameConsentScope(scope.operations, [`binding:${target!.action}`, 'credential:session'])
      && scope.capabilities.length === 0 && scope.sources.length === 1 && scope.sources[0]?.record_type === 'credential'
      && (target!.action === 'link' ? scope.setup_action === 'link'
        && sameConsentScope(scope.native_resource_ref, target!.native_resource_ref)
        && sameConsentScope(scope.sources[0], { ...(configuration.settings.devops!.hosting_connection_ref as RecordReference), record_type: 'credential' })
        && scope.credential_id === target!.native_resource_ref.source_connection_id
        : (scope.setup_action === undefined || scope.setup_action === 'create') && scope.native_resource_ref == null)
      : !scope.control_action && !scope.purpose)
    && Array.isArray(scope.capabilities) && (setup || scope.capabilities.length > 0) && scope.capabilities.every(item => typeof item === 'string')
    && Array.isArray(scope.operations) && scope.operations.length > 0 && scope.operations.every(item => typeof item === 'string')
    && Array.isArray(scope.sources) && scope.sources.length > 0
    && scope.sources.every(source => source && source.tenant_id === scope.tenant_id && source.project_id === scope.project_id
      && ['source_connection_id', 'record_type', 'record_id', 'record_revision'].every(field => typeof source[field as keyof RecordReference] === 'string' && source[field as keyof RecordReference]))
    && /^[a-f0-9]{64}$/.test(result.value.scope_digest) && result.value.maximum_lifetime_seconds === 86400);
}
