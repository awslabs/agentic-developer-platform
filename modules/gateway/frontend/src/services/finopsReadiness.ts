import { apiClient } from './api';
import { finOpsPath, type FinOpsBinding, type FinOpsConfiguration } from './l5Services';

export const checkFinOpsConfiguration = (productId: string, projectId: string) =>
  apiClient.post<{ ready: boolean; gaps: string[] }>(`${finOpsPath(productId, projectId)}/configuration/check`);

export function finOpsReadinessAction(code: string): string {
  if (code === 'finops_admissions_disabled') return 'Cost inquiries: Not available yet in this environment. A platform administrator must qualify FinOps execution before enabling inquiries. You can continue preparing setup and reading retained results.';
  if (code === 'work_queue_unavailable') return 'Ask the platform administrator to restore the FinOps work queue configuration, then refresh availability and check readiness again.';
  if (code === 'finops_authority_unavailable') return 'Ask the platform administrator to restore the FinOps execution integration, then check readiness again.';
  if (code === 'native_unqualified') return 'The selected FinOps agent or its billing access is not qualified. Ask the platform administrator to verify the native executor and adoption readback before checking readiness again.';
  if (code === 'execution_delegation') return 'The Product owner must review and grant execution consent for this saved configuration and binding in Setup.';
  return 'Resolve this reported setup requirement, then check FinOps readiness again.';
}

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const sameReference = (left: unknown, right: unknown) => {
  const a = record(left), b = record(right);
  return ['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision']
    .every(field => typeof a[field] === 'string' && a[field] !== '' && a[field] === b[field]);
};

/** Match the billing readback the backend exposes; admission checks it again. */
export function finOpsBindingReady(binding: FinOpsBinding, configuration: FinOpsConfiguration): boolean {
  const selected = record(configuration.settings.finops);
  const observed = binding as FinOpsBinding & { native_configuration_revision?: number; native_agent_display_name?: string; billing_scope?: unknown };
  const billing = record(selected.billing_scope), readback = record(observed.billing_scope);
  const accounts = billing.account_ids, observedAccounts = readback.account_ids;
  return binding.state === 'ready' && Array.isArray(binding.gaps) && binding.gaps.length === 0
    && binding.configuration_id === configuration.configuration_id && observed.native_configuration_revision === configuration.revision
    && ['tenant_id', 'product_id', 'project_id', 'relationship_revision'].every(field =>
      record(binding.scope)[field] === record(configuration.scope)[field])
    && sameReference(binding.connection_ref, configuration.settings.connection_ref)
    && binding.native_resource_ref?.record_type === 'finops_agent'
    && Boolean(selected.native_agent_ref) && binding.native_resource_ref?.record_id === selected.native_agent_ref
    && Boolean(observed.native_agent_display_name)
    && typeof billing.view_ref === 'string' && billing.view_ref !== '' && billing.view_ref === readback.view_ref
    && Array.isArray(accounts) && accounts.length > 0 && Array.isArray(observedAccounts)
    && accounts.length === observedAccounts.length && accounts.every((account, index) => account === observedAccounts[index]);
}
