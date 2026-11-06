import type { AccessPolicyReadback, ResourceBinding, ResourceConsentReceipt, ResourceReadback, SetupConfiguration } from '@/services/devOpsActivation';
import type { OwnerGrant } from '@/services/l5OwnerConsent';

export const reference = (type = 'connection', id = 'connection') => ({ tenant_id: 'tenant', project_id: 'project',
  source_connection_id: 'connection', record_type: type, record_id: id, record_revision: 'revision' });
export const configuration: SetupConfiguration = {
  configuration_id: 'config', revision: 3, hash: 'config-hash', state: 'needs_input', handoff_state: 'linked', inherited_fields: [], gaps: [],
  scope: { tenant_id: 'tenant', product_id: 'product', project_id: 'project', relationship_revision: '1', owner_id: 'owner' },
  settings: { service_id: 'devops', profile_ref: reference('product', 'product'), source_refs: [reference('environment', 'environment')],
    capabilities: ['investigation'], enabled: false, visibility: 'only_me', exclusions: [], overrides: [], triggers: [],
    devops: { hosting_connection_ref: reference(), region: 'us-east-1', space_mode: 'create', access_policy_ref: reference('policy', 'old-policy'),
      accounts: [{ account_id: '123456789012', environments: [{ environment_ref: reference('environment', 'environment') }] }],
      integrations: [], authentication_mode: 'scoped_iam_federation', permitted_native_actions: [], trigger_policies: [], prevention_enabled: false,
      retention_days: 30, browser_launch_policy: 'disabled', knowledge_policy: { source_refs: [], visibility: 'only_me', publish_results: false, refresh: 'manual' } } },
};
export const binding: ResourceBinding = { binding_id: 'binding', configuration_id: 'config', scope: configuration.scope, generation: 2, revision: 4,
  state: 'ready', ownership: 'adp_created', connection_ref: reference(), native_resource_ref: reference('agent_space', 'native-space'), gaps: [],
  provider_identity: { account_id: '123456789012', region: 'us-east-1', role_arn: 'arn:aws:iam::123456789012:role/setup' } };
export const selection = { ...binding.provider_identity!, setup_role_arn: binding.provider_identity!.role_arn,
  role_arn: 'arn:aws:iam::123456789012:role/resource', connection_ref: reference('credential'), workload_kind: 'lambda' as const,
  cluster_name: null, namespace: null, resource_arn: 'arn:aws:lambda:us-east-1:123456789012:function:selected',
  telemetry_arn: 'arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/selected:*' };
export const consent: ResourceConsentReceipt = { key: 'original_consent_key', etag: '"config-hash"', state: 'confirmed',
  body: { binding_id: 'binding', for_control: true, control_action: 'devops_resource_verification', resource_selection: selection,
    expires_at: '2030-01-01T01:00:00.000Z', scope_digest: 'a'.repeat(64) },
  scope: { tenant_id: 'tenant', product_id: 'product', project_id: 'project', service_id: 'l5-devops-worker',
    configuration_id: 'config', configuration_revision: 3, configuration_hash: 'config-hash', created_by: 'owner',
    credential_id: 'connection', provider_account_id: '123456789012', provider_role_arn: selection.setup_role_arn, region: selection.region,
    binding_id: 'binding', binding_generation: 3, operations: ['binding:verify-resource', 'credential:session'], capabilities: [],
    sources: [selection.connection_ref], visibility: 'only_me', purpose: 'devops_resource_verification', control_action: 'devops_resource_verification',
    resource_selection: selection },
};
export const grant: OwnerGrant = { delegation_id: 'grant', revision: 1, tenant_id: 'tenant', product_id: 'product', project_id: 'project',
  service_id: 'l5-devops-worker', credential_id: 'connection', binding_generation: 3, operations: consent.scope.operations, capabilities: [],
  sources: consent.scope.sources, visibility: 'only_me', execution_scope: consent.scope, expires_at: consent.body.expires_at, revoked_at: null, active: true };
export const resources: ResourceReadback = { binding_id: 'binding', native_readiness: 'verifying',
  resource_refs: [reference('resource', selection.resource_arn)], telemetry_refs: [reference('telemetry', selection.telemetry_arn)] };
export const policy: AccessPolicyReadback = { policy_ref: reference('policy', 'verified-policy'), native_readiness: 'verifying',
  document: { binding_id: 'binding', binding_generation: 2, binding_revision: 4, scope: configuration.scope, hosting_connection_ref: reference(),
    operation_id: 'verified-policy', native_space_id: 'native-space', configuration_revision: 3,
    resource_observation: { checked_at: '2030-01-01T00:00:00Z', expires_at: '2030-01-01T00:15:00Z', resource_selection: selection } } };
export const limits = { classification: 'internal', maxCost: '1.25', deadline: 300, polls: 10 };
export const storageKey = 'adp:devops-activation:["tenant","product","project","config","owner"]';
