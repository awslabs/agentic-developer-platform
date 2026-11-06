import { apiClient } from './api';
import { devOpsPath, type DevOpsBinding, type DevOpsConfiguration, type Versioned } from './l5Services';
import { sameConsentScope } from './l5OwnerConsent';

export interface InvestigationReadiness {
  ready: boolean; gaps: string[]; scope: DevOpsConfiguration['scope']; capability: 'investigation';
  configuration_id: string; configuration_revision: number; configuration_hash: string;
  binding_id?: string; binding_generation?: number; binding_revision?: number; checked_at?: string; valid_until?: string;
}
export const checkInvestigationReadiness = (productId: string, projectId: string) =>
  apiClient.post<InvestigationReadiness>(`${devOpsPath(productId, projectId)}/configuration/check`, {});
export function investigationReadbackMatches(value: InvestigationReadiness, configuration: Versioned<DevOpsConfiguration>, binding: Versioned<DevOpsBinding>) {
  return sameConsentScope(value.scope, configuration.value.scope) && sameConsentScope(binding.value.scope, configuration.value.scope)
    && value.capability === 'investigation' && value.configuration_id === configuration.value.configuration_id
    && value.configuration_revision === configuration.value.revision && value.configuration_hash === configuration.value.hash
    && typeof value.ready === 'boolean' && Array.isArray(value.gaps) && value.gaps.every(code => typeof code === 'string')
    && (!value.ready || (value.binding_id === binding.value.binding_id && value.binding_generation === binding.value.generation
      && value.binding_revision === binding.value.revision && value.gaps.length === 0
      && Number.isFinite(Date.parse(value.checked_at ?? '')) && Date.parse(value.checked_at!) <= Date.now()
      && Date.parse(value.valid_until ?? '') > Date.now() && Date.parse(value.valid_until!) <= Date.parse(value.checked_at!) + 60_000));
}
export function investigationGapAction(code: string) {
  if (code === 'association_discovery_denied') return 'AWS denied listing workload connections. Ask the connection administrator to verify aidevops:ListAssociations access to the selected agent space, then verify resource access again.';
  if (code === 'association_readback_denied') return 'AWS denied reading the workload connection. Ask the connection administrator to verify aidevops:GetAssociation access to the selected agent space, then verify resource access again.';
  if (code === 'association_discovery_unavailable' || code === 'association_discovery_incomplete') return 'ADP could not read the complete list of workload connections. Verify resource access again; if this persists, ask the platform administrator to inspect the ListAssociations failure in the DevOps worker logs.';
  if (code === 'association_readback_unavailable') return 'ADP found the workload connection but could not verify its details. Verify resource access again; if this persists, ask the platform administrator to inspect the GetAssociation failure in the DevOps worker logs.';
  if (code === 'association_outcome_unknown') return 'ADP could not confirm the workload connection after an interrupted setup. Ask the connection administrator to reconcile the existing association, then verify resource access again.';
  if (code === 'readiness_expired') return 'This readiness snapshot expired. Check Investigation readiness again before submitting.';
  if (code === 'integration_not_ready') return 'Review the selected integration and its current provider access in Setup. Resolve its reported connection or event support gaps, then check Investigation readiness again.';
  if (code === 'binding_or_delegation_unavailable') return 'Refresh the selected binding and resolve its setup gaps. The Product owner can then review execution consent for the current binding.';
  if (code === 'delegation_scope_changed') return 'The saved configuration, binding or verified resources changed. Refresh the resource readback in Setup, then ask the Product owner to review and renew execution consent.';
  if (/delegation|execution_scope|consent/.test(code)) return 'The Product owner must review and grant execution consent for this saved revision and binding in Setup.';
  if (/resource|access_policy|source/.test(code)) return 'Read the verified workload references in Setup. If their evidence expired or changed, verify the selected resource access again.';
  if (/policy|budget|limit/.test(code)) return 'Review the saved investigation time limits and workload access policy before checking again.';
  if (/queue|provider|runtime/.test(code)) return 'Ask the platform administrator to restore the DevOps worker and its required runtime configuration, then check again.';
  if (/connection|credential/.test(code)) return 'Verify the selected AWS connection and its current revision, then refresh the binding.';
  if (/trigger/.test(code)) return 'Review the enabled manual Investigation trigger for this binding and its exact resources in Setup.';
  if (/configuration/.test(code)) return 'Complete and save Investigation activation in Setup, then refresh this configuration.';
  if (/binding/.test(code)) return 'Refresh the selected binding and resolve its setup gaps before checking Investigation again.';
  return 'Review this readiness reason with the Product owner, resolve it in Setup, then check Investigation again.';
}
