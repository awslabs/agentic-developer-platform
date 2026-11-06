import { apiClient } from './api';
import { getSecurityBinding, getSecurityConfiguration, securityPath, type SecurityBinding, type SecurityConfiguration, type Versioned } from './l5Services';
import { sameConsentScope } from './l5OwnerConsent';
import { securityCapabilityReady } from './securityCapabilityReadiness';

export interface SecurityCheck { ready: boolean; gaps: string[]; provider_contacted: boolean }

export async function checkSecurityReadiness(productId: string, projectId: string,
  configuration: Versioned<SecurityConfiguration>, binding: SecurityBinding, capability: string): Promise<SecurityCheck> {
  const result = await apiClient.post<SecurityCheck>(`${securityPath(productId, projectId)}/configuration/check`, {});
  if (!result || typeof result.ready !== 'boolean' || typeof result.provider_contacted !== 'boolean'
    || !Array.isArray(result.gaps) || result.gaps.some(code => typeof code !== 'string') || result.ready && result.gaps.length) {
    throw new Error('Security readiness response is incomplete');
  }
  if (!result.ready) return result;
  // The check response has no identity fields. Read both identities afterwards
  // so a newer configuration or binding cannot qualify the user's old choices.
  const [saved, current] = await Promise.all([
    getSecurityConfiguration(productId, projectId), getSecurityBinding(productId, projectId, binding.binding_id),
  ]);
  if (typeof configuration.value.hash !== 'string' || !configuration.value.hash
    || !Number.isSafeInteger(configuration.value.revision) || configuration.value.revision < 1
    || !Number.isSafeInteger(binding.revision) || binding.revision < 1 || !Number.isSafeInteger(binding.generation) || binding.generation < 1
    || saved.value.configuration_id !== configuration.value.configuration_id
    || saved.value.revision !== configuration.value.revision || saved.value.hash !== configuration.value.hash
    || !sameConsentScope(saved.value.scope, configuration.value.scope)
    || !sameConsentScope(current.value.scope, configuration.value.scope)
    || !sameConsentScope(binding.scope, configuration.value.scope)
    || current.value.configuration_id !== configuration.value.configuration_id || current.value.binding_id !== binding.binding_id
    || current.value.generation !== binding.generation || current.value.revision !== binding.revision
    || !securityCapabilityReady(current.value, capability)) throw new Error('Security readiness selection changed');
  return result;
}

export function securityReadinessAction(code: string) {
  if (code === 'readiness_expired') return 'This readiness check expired. Check Security readiness again before starting an assessment.';
  if (/work_queue|runtime/.test(code)) return 'Ask the platform administrator to restore the Security worker and its runtime configuration, then check readiness again.';
  if (/delegation|consent/.test(code)) return 'The Product owner must review and renew execution consent for this saved configuration and binding in Setup.';
  if (/profile/.test(code)) return 'Open Product profile and review its changes before checking readiness again.';
  if (/connection/.test(code)) return 'Verify the selected AWS connection and its current revision, then refresh assessment status.';
  if (/source/.test(code)) return 'Review the pinned source revisions and source registration in Setup, then check the existing binding.';
  if (/native|binding/.test(code)) return 'Inspect source registration and the selected threat model in Setup, then check the existing binding and refresh assessment status.';
  if (/limit|budget|policy/.test(code)) return 'Review the saved assessment limits and organization budget policy before checking readiness again.';
  if (/configuration|capability|trigger/.test(code)) return 'Review and save the enabled Security configuration, capability and matching manual trigger in Setup.';
  return 'Review this readiness reason with the Product owner and resolve it in Setup before checking again.';
}
