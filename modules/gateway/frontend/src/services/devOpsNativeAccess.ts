import { apiClient } from './api';
import { devOpsPath, type DevOpsBinding, type DevOpsConfiguration, type Versioned } from './l5Services';
import { sameConsentScope, type ConsentPreview, type OwnerGrant } from './l5OwnerConsent';

export interface NativePolicy { launcher_role_arn: string; operator_role_arn: string }
export interface NativeEligibility {
  ready: boolean; gaps: string[]; capability: 'native_ui'; scope: DevOpsConfiguration['scope'];
  configuration_id: string; configuration_revision: number; configuration_hash: string;
  binding_id: string; binding_generation: number; binding_revision: number; checked_at: string; valid_until: string;
}
export interface NativeConsentRequest {
  binding_id: string; for_control: true; control_action: 'devops_native_ui'; native_ui_policy: NativePolicy;
  scope_digest: string; expires_at: string;
}
export const checkNativeEligibility = (productId: string, projectId: string, bindingId: string, revision: number) =>
  apiClient.post<NativeEligibility>(`${devOpsPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}/browser-launch/check`, { configuration_revision: revision });
export const previewNativeConsent = (productId: string, projectId: string, bindingId: string, policy: NativePolicy) =>
  apiClient.requestWithEtag<ConsentPreview>(`${devOpsPath(productId, projectId)}/delegation-scope`, {
    method: 'POST', body: { binding_id: bindingId, for_control: true, control_action: 'devops_native_ui', native_ui_policy: policy },
  });
export const grantNativeConsent = (productId: string, projectId: string, body: NativeConsentRequest, etag: string, key: string) =>
  apiClient.requestWithEtag<OwnerGrant>(`${devOpsPath(productId, projectId)}/delegations`, {
    method: 'POST', headers: { 'If-Match': etag, 'Idempotency-Key': key }, body,
  });
export function nativeEligibilityMatches(value: NativeEligibility, configuration: Versioned<DevOpsConfiguration>, binding: Versioned<DevOpsBinding>) {
  return value.ready === true && value.capability === 'native_ui' && Array.isArray(value.gaps) && value.gaps.length === 0
    && sameConsentScope(value.scope, configuration.value.scope) && sameConsentScope(binding.value.scope, configuration.value.scope)
    && value.configuration_id === configuration.value.configuration_id && value.configuration_revision === configuration.value.revision
    && value.configuration_hash === configuration.value.hash && value.binding_id === binding.value.binding_id
    && value.binding_generation === binding.value.generation && value.binding_revision === binding.value.revision
    && Number.isFinite(Date.parse(value.checked_at)) && Date.parse(value.checked_at) <= Date.now()
    && Date.parse(value.valid_until) > Date.now() && Date.parse(value.valid_until) <= Date.parse(value.checked_at) + 60_000;
}
export function nativeConsentMatches(value: ConsentPreview, configuration: Versioned<DevOpsConfiguration>, binding: Versioned<DevOpsBinding>, policy: NativePolicy) {
  const scope = value.scope; const selected = scope.native_ui_policy as Record<string, unknown> | undefined;
  return scope.purpose === 'devops_native_ui' && scope.control_action === 'devops_native_ui' && scope.service_id === 'l5-devops-worker'
    && scope.tenant_id === configuration.value.scope.tenant_id && scope.product_id === configuration.value.scope.product_id
    && scope.project_id === configuration.value.scope.project_id && scope.created_by === configuration.value.scope.owner_id
    && scope.configuration_id === configuration.value.configuration_id && scope.configuration_revision === configuration.value.revision
    && scope.configuration_hash === configuration.value.hash && scope.binding_id === binding.value.binding_id
    && scope.binding_generation === binding.value.generation && selected?.binding_revision === binding.value.revision
    && scope.provider_account_id === binding.value.provider_identity?.account_id
    && scope.provider_role_arn === binding.value.provider_identity?.role_arn && scope.region === binding.value.provider_identity?.region
    && selected?.authentication_mode === 'scoped_iam_federation' && selected?.browser_launch_policy === 'scoped_session'
    && typeof selected?.setup_operation_id === 'string' && selected.setup_operation_id.length > 0
    && binding.value.state === 'ready' && binding.value.gaps.length === 0 && sameConsentScope(binding.value.scope, configuration.value.scope)
    && selected?.launcher_role_arn === policy.launcher_role_arn && selected?.operator_role_arn === policy.operator_role_arn
    && sameConsentScope(selected?.native_space_ref, binding.value.native_resource_ref)
    && sameConsentScope(scope.operations, ['credential:session']) && sameConsentScope(scope.capabilities, ['native_ui'])
    && sameConsentScope(selected?.permitted_native_actions, ['launchNativeBrowser'])
    && sameConsentScope(scope.sources, [{ ...binding.value.connection_ref, record_type: 'credential' }])
    && /^[a-f0-9]{64}$/.test(value.scope_digest) && value.maximum_lifetime_seconds === 86400;
}
export function nativeAccessError(error: unknown) {
  const code = (error as { detail?: { code?: string } })?.detail?.code;
  if (code === 'delegation_expiry_invalid') return 'Choose a native UI consent expiry more than 1 minute and no more than 24 hours from now, then review the scope again.';
  if (code === 'native_operator_app_unavailable') return 'Verify that the selected native DevOps space has IAM app access enabled with this operator role, then check access again.';
  if (code === 'launch_configuration_unavailable') return 'Ask the platform administrator to verify the ADP HTTPS issuer and distinct launcher/operator roles for this hosting account.';
  if (code === 'federation_unavailable') return 'AWS did not issue a usable sign-in handoff. Verify the launcher role trust and selected-space permissions, then check access again.';
  if (code === 'session_required') return 'Sign in to ADP again, then check native UI access.';
  if (code === 'not_found') return 'SSO is not ready for this selection. The Product owner should select the connected space and review Native UI owner consent in AWS connection and access. The platform administrator must enable its AWS operator app and configure the separate launcher and operator roles.';
  if (code === 'native_ui_scope_invalid') return 'Select a ready binding and distinct launcher and operator roles from its verified hosting account.';
  if (code?.includes('delegation') || code?.includes('consent')) return 'Review the current native UI grant in Setup. The Product owner must grant this exact space, role policy and expiry separately from Investigation.';
  if (code === 'selected_connection_unavailable') return 'Verify the saved hosting connection and refresh this binding before checking native UI access again.';
  return 'Native UI access could not be confirmed. Refresh the binding, review its native UI consent and check your ADP session. No native arrival has been recorded.';
}
