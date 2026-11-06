import { apiClient } from './api';
import { devOpsPath, type DevOpsConfiguration, type DevOpsSettings, type SecurityBindingOperation, type Versioned } from './l5Services';
import { consentScopeMatches, devOpsSetupDraft, devOpsSetupTarget, sameConsentScope, type ConsentPreview, type ConsentScope, type OwnerGrant } from './l5OwnerConsent';
import type { RecordReference } from './products';

export type SetupConfiguration = DevOpsConfiguration & { scope: DevOpsConfiguration['scope'] & { owner_id?: string } };
export interface ResourceBinding {
  binding_id: string; configuration_id: string; scope: SetupConfiguration['scope']; generation: number; revision: number;
  state: string; ownership: string; connection_ref: RecordReference; gaps: string[];
  native_resource_ref?: RecordReference;
  provider_identity?: { account_id: string; region: string; role_arn: string };
}
export interface ResourceSelection {
  account_id: string; region: string; role_arn: string; setup_role_arn: string; connection_ref: RecordReference;
  workload_kind: 'lambda'; cluster_name: null; namespace: null; resource_arn: string; telemetry_arn: string;
}
export interface ResourceConsentSelection {
  binding_id: string; for_control: true; control_action: 'devops_resource_verification'; resource_selection: ResourceSelection;
}
export interface ResourceConsentReceipt {
  key: string; etag: string; scope: ConsentScope;
  body: ResourceConsentSelection & { scope_digest: string; expires_at: string };
  state: 'pending' | 'confirmed' | 'rejected';
}
export interface ResourceCheckReceipt {
  key: string; etag: string; body: { action: 'check'; configuration_revision: number; binding_id: string; resource_selection: ResourceSelection };
  state: 'pending' | 'confirmed' | 'rejected'; operation?: SecurityBindingOperation;
}
export interface ResourceReadback { binding_id: string; native_readiness: string; resource_refs: RecordReference[]; telemetry_refs: RecordReference[] }
export interface AccessPolicyReadback {
  policy_ref: RecordReference; native_readiness: string;
  document: { binding_id: string; binding_generation: number; binding_revision: number; scope: SetupConfiguration['scope'];
    operation_id: string; native_space_id: string; configuration_revision: number;
    hosting_connection_ref: RecordReference; resource_observation: { checked_at: string; expires_at: string; resource_selection: ResourceSelection } };
}
export type ActivationSettings = DevOpsSettings & {
  classification: string;
  limits: { max_cost: string; currency: 'USD'; deadline_seconds: number; max_provider_attempts: 1; max_polls: number; concurrency: 1; policy_ref?: RecordReference };
};
export interface ActivationReceipt {
  key: string; etag: string; configuration_id: string; previous_revision: number; body: ActivationSettings;
  state: 'pending' | 'confirmed' | 'rejected';
}
export interface SetupJournal { consent?: ResourceConsentReceipt; check?: ResourceCheckReceipt; activation?: ActivationReceipt }

export const listResourceBindings = (productId: string, projectId: string) =>
  apiClient.get<{ items: ResourceBinding[] }>(`${devOpsPath(productId, projectId)}/bindings`);
export const readResourceBinding = (productId: string, projectId: string, bindingId: string) =>
  apiClient.requestWithEtag<ResourceBinding>(`${devOpsPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}`);
export const previewResourceConsent = (productId: string, projectId: string, body: ResourceConsentSelection) =>
  apiClient.requestWithEtag<ConsentPreview>(`${devOpsPath(productId, projectId)}/delegation-scope`, { method: 'POST', body });
export const grantResourceConsent = (productId: string, projectId: string, receipt: ResourceConsentReceipt) =>
  apiClient.requestWithEtag<OwnerGrant>(`${devOpsPath(productId, projectId)}/delegations`, {
    method: 'POST', body: receipt.body, headers: { 'If-Match': receipt.etag, 'Idempotency-Key': receipt.key },
  });
export const verifyResource = (productId: string, projectId: string, receipt: ResourceCheckReceipt) =>
  apiClient.requestWithEtag<SecurityBindingOperation>(`${devOpsPath(productId, projectId)}/binding-operations`, {
    method: 'POST', body: receipt.body, headers: { 'If-Match': receipt.etag, 'Idempotency-Key': receipt.key },
  });
export const readVerifiedResources = (productId: string, projectId: string, bindingId: string) =>
  apiClient.get<ResourceReadback>(`${devOpsPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}/resources`);
export const readResourceAccessPolicy = (productId: string, projectId: string, bindingId: string) =>
  apiClient.get<AccessPolicyReadback>(`${devOpsPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}/access-policy`);
export const activateInvestigation = (productId: string, projectId: string, receipt: ActivationReceipt) =>
  apiClient.requestWithEtag<SetupConfiguration>(`${devOpsPath(productId, projectId)}/configuration`, {
    method: 'PUT', body: receipt.body, headers: { 'If-Match': receipt.etag, 'Idempotency-Key': receipt.key },
  });
export const readDevOpsConfigurationVersion = (productId: string, projectId: string, revision: number) =>
  apiClient.requestWithEtag<SetupConfiguration>(`${devOpsPath(productId, projectId)}/configuration/versions/${revision}`);

export function referenceValid(value: unknown, configuration: SetupConfiguration, kind?: string): value is RecordReference {
  if (!value || typeof value !== 'object') return false;
  const ref = value as RecordReference;
  return sameConsentScope(Object.keys(ref).sort(), ['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'].sort())
    && Object.values(ref).every(item => typeof item === 'string' && item.length > 0)
    && ref.tenant_id === configuration.scope.tenant_id && ref.project_id === configuration.scope.project_id && (!kind || ref.record_type === kind);
}
export function bindingMatches(binding: ResourceBinding, configuration: SetupConfiguration) {
  const target = devOpsSetupTarget(configuration.settings, configuration.scope);
  return sameConsentScope(binding.scope, configuration.scope) && Boolean(configuration.scope.owner_id?.trim())
    && binding.configuration_id === configuration.configuration_id && target !== null
    && (target.action === 'create' ? binding.ownership === 'adp_created' : binding.ownership === 'customer_linked'
      && sameConsentScope(binding.native_resource_ref, target.native_resource_ref))
    && ['ready', 'blocked', 'checking'].includes(binding.state) && Number.isInteger(binding.generation) && binding.generation > 0
    && sameConsentScope(binding.connection_ref, configuration.settings.devops.hosting_connection_ref)
    && binding.provider_identity?.region === configuration.settings.devops.region;
}
export function resourceSelection(configuration: SetupConfiguration, binding: ResourceBinding, role: string, lambda: string, logs: string): ResourceSelection {
  const identity = binding.provider_identity;
  if (!devOpsSetupDraft(configuration.settings, configuration.scope) || !bindingMatches(binding, configuration) || !identity
    || !/^\d{12}$/.test(identity.account_id)) throw new Error('Read the created or adopted binding matching the saved disabled Investigation draft and exact space selection.');
  const rolePattern = new RegExp(`^arn:aws:iam::${identity.account_id}:role/[A-Za-z0-9+=,.@_/-]+$`);
  const functionPrefix = `arn:aws:lambda:${identity.region}:${identity.account_id}:function:`;
  const logsPrefix = `arn:aws:logs:${identity.region}:${identity.account_id}:log-group:`;
  if (!rolePattern.test(role) || !rolePattern.test(identity.role_arn) || role === identity.role_arn
    || !lambda.startsWith(functionPrefix) || !/^[A-Za-z0-9_-]+$/.test(lambda.slice(functionPrefix.length))
    || !logs.startsWith(logsPrefix) || !logs.endsWith(':*') || logs.slice(0, -2).includes('*') || !logs.slice(logsPrefix.length, -2)) {
    throw new Error('Select a distinct resource role, one unqualified Lambda ARN and its log-group ARN ending in :*, all in the binding account and Region.');
  }
  return { account_id: identity.account_id, region: identity.region, setup_role_arn: identity.role_arn, role_arn: role,
    connection_ref: { ...binding.connection_ref, record_type: 'credential' }, workload_kind: 'lambda', cluster_name: null, namespace: null,
    resource_arn: lambda, telemetry_arn: logs };
}
export function validResourcePreview(result: Versioned<ConsentPreview>, config: SetupConfiguration, binding: ResourceBinding, selection: ResourceSelection) {
  const scope = result.value?.scope;
  return Boolean(scope && devOpsSetupDraft(config.settings, config.scope) && bindingMatches(binding, config) && consentScopeMatches(scope, config)
    && config.scope.owner_id?.trim() && scope.created_by === config.scope.owner_id && scope.service_id === 'l5-devops-worker'
    && scope.configuration_id === config.configuration_id && scope.configuration_revision === config.revision
    && scope.configuration_hash === config.hash && result.etag === `"${config.hash}"`
    && scope.binding_id === binding.binding_id && scope.binding_generation === binding.generation + 1
    && scope.provider_account_id === selection.account_id && scope.provider_role_arn === selection.setup_role_arn && scope.region === selection.region
    && scope.control_action === 'devops_resource_verification' && scope.purpose === 'devops_resource_verification'
    && sameConsentScope(scope.operations, ['binding:verify-resource', 'credential:session']) && sameConsentScope(scope.capabilities, [])
    && sameConsentScope(scope.sources, [selection.connection_ref]) && sameConsentScope(scope.resource_selection, selection)
    && /^[a-f0-9]{64}$/.test(result.value.scope_digest) && result.value.maximum_lifetime_seconds === 86400);
}
export const resourceGrantMatches = (grant: OwnerGrant, receipt: ResourceConsentReceipt) =>
  sameConsentScope(grant.execution_scope, receipt.scope) && Date.parse(grant.expires_at) === Date.parse(receipt.body.expires_at);

// Validate before displaying any returned references as well as before using them.
export function validateResourceReadback(config: SetupConfiguration, binding: ResourceBinding, resources: ResourceReadback,
  policy: AccessPolicyReadback, now = Date.now()) {
  const observed = policy.document.resource_observation;
  if (!devOpsSetupDraft(config.settings, config.scope) || !bindingMatches(binding, config) || binding.state !== 'ready' || binding.gaps.length
    || !referenceValid(binding.native_resource_ref, config, 'agent_space')
    || binding.native_resource_ref.source_connection_id !== binding.connection_ref.source_connection_id
    || policy.document.native_space_id !== binding.native_resource_ref.record_id
    || !Number.isInteger(policy.document.configuration_revision) || policy.document.configuration_revision < 1 || policy.document.configuration_revision > config.revision
    || resources.binding_id !== binding.binding_id || policy.document.binding_id !== binding.binding_id
    || policy.document.binding_generation !== binding.generation || policy.document.binding_revision !== binding.revision
    || !sameConsentScope(policy.document.scope, binding.scope) || !sameConsentScope(policy.document.hosting_connection_ref, binding.connection_ref)
    || !referenceValid(policy.policy_ref, config, 'policy') || policy.policy_ref.source_connection_id !== binding.connection_ref.source_connection_id
    || policy.policy_ref.record_id !== policy.document.operation_id || !['verifying', 'ready'].includes(resources.native_readiness)
    || !['verifying', 'ready'].includes(policy.native_readiness)
    || !(Date.parse(observed.checked_at) <= now && now - Date.parse(observed.checked_at) <= 15 * 60_000 && now < Date.parse(observed.expires_at))
    || !sameConsentScope(observed.resource_selection, resourceSelection(config, binding, observed.resource_selection.role_arn,
      observed.resource_selection.resource_arn, observed.resource_selection.telemetry_arn))
    || resources.resource_refs.length !== 1 || resources.telemetry_refs.length !== 1
    || resources.resource_refs[0].record_revision !== resources.telemetry_refs[0].record_revision
    || !resources.resource_refs.every(ref => referenceValid(ref, config, 'resource') && ref.source_connection_id === binding.connection_ref.source_connection_id && ref.record_id === observed.resource_selection.resource_arn)
    || !resources.telemetry_refs.every(ref => referenceValid(ref, config, 'telemetry') && ref.source_connection_id === binding.connection_ref.source_connection_id && ref.record_id === observed.resource_selection.telemetry_arn)) {
    throw new Error('Read fresh verified resources and access policy for the same ready binding and unchanged saved environment. Verify resource access again if evidence expired.');
  }
}
export function buildActivation(config: SetupConfiguration, binding: ResourceBinding, resources: ResourceReadback,
  policy: AccessPolicyReadback, input: { classification: string; maxCost: string; deadline: number; polls: number }, now = Date.now()): ActivationSettings {
  validateResourceReadback(config, binding, resources, policy, now);
  const settings = config.settings;
  const accounts = settings.devops.accounts as { account_id: string; environments: { environment_ref: RecordReference; resource_refs?: RecordReference[] }[] }[];
  const environment = settings.source_refs[0];
  if (accounts.length !== 1 || accounts[0].account_id !== binding.provider_identity?.account_id || accounts[0].environments?.length !== 1
    || settings.source_refs.length !== 1 || !referenceValid(environment, config, 'environment')
    || !sameConsentScope(accounts[0].environments[0].environment_ref, environment)
    || environment.source_connection_id !== binding.connection_ref.record_id) {
    throw new Error('Select the original saved account and environment before reviewing activation.');
  }
  if (!input.classification.trim() || !/^\d+(?:\.\d+)?$/.test(input.maxCost) || !Number.isFinite(Number(input.maxCost)) || Number(input.maxCost) <= 0
    || !Number.isInteger(input.deadline) || input.deadline < 60 || input.deadline > 3600
    || !Number.isInteger(input.polls) || input.polls < 1 || input.polls > 60) {
    throw new Error('Set a classification, positive USD planning estimate, deadline of 60–3600 seconds and 1–60 polls.');
  }
  const classification = input.classification.trim();
  const overrides = classification === settings.classification ? settings.overrides : [
    ...settings.overrides.filter(override => override.field !== 'classification'),
    { field: 'classification', value: classification, reason: 'Classification selected for this manual investigation during activation.' },
  ];
  return { ...settings, enabled: true, capabilities: ['investigation'], classification, overrides,
    limits: { max_cost: input.maxCost, currency: 'USD', deadline_seconds: input.deadline, max_provider_attempts: 1, max_polls: input.polls,
      concurrency: 1 },
    devops: { ...settings.devops, access_policy_ref: policy.policy_ref,
      accounts: [{ ...accounts[0], environments: [{ ...accounts[0].environments[0], resource_refs: resources.resource_refs }] }],
      trigger_policies: [{ kind: 'manual', capability: 'investigation', enabled: true, binding_id: binding.binding_id,
        environment_ref: environment, resource_refs: resources.resource_refs, telemetry_refs: resources.telemetry_refs }] } };
}
export function activationMatches(result: Versioned<SetupConfiguration>, config: SetupConfiguration, receipt: ActivationReceipt) {
  // The server can normalize monitoring independently; every submitted setting must still match.
  const expected = { ...receipt.body } as Record<string, unknown>;
  const actual = { ...result.value.settings } as Record<string, unknown>;
  delete expected.monitoring; delete actual.monitoring;
  return consentScopeMatches(result.value.scope, config) && result.value.scope.owner_id === config.scope.owner_id
    && result.value.configuration_id === receipt.configuration_id && result.value.revision === receipt.previous_revision + 1
    && result.etag === `"${result.value.hash}"` && sameConsentScope(actual, expected);
}
