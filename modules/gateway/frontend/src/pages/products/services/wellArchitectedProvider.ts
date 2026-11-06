import type { WellArchitectedProviderInput, WellArchitectedProviderSelection } from '@/services/l5Services';
import type { RecordReference } from '@/services/products';

const fields = ['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'] as const;
export const sameReference = (left: RecordReference, right?: RecordReference) =>
  !!right && fields.every(field => left[field] === right[field]);

export function scopedReference(value: unknown, tenantId: string, projectId: string): value is RecordReference {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const ref = value as RecordReference;
  return Object.keys(ref).length === fields.length
    && fields.every(field => typeof ref[field] === 'string' && !!ref[field].trim() && !['latest', 'null', 'HEAD'].includes(ref[field]))
    && ref.tenant_id === tenantId && ref.project_id === projectId;
}

export const validProviderInput = (input: WellArchitectedProviderInput) =>
  /^[a-z]{2}(?:-[a-z]+)+-[0-9]+$/.test(input.region)
  && /^arn:aws:iam::[0-9]{12}:role\/[A-Za-z0-9+=,.@_/-]+$/.test(input.execution_role_arn)
  && /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(input.source_bucket);

/** Provider values are accepted only as the exact scoped response to this explicit selection. */
export function validProviderSelection(value: unknown, input: WellArchitectedProviderInput): value is WellArchitectedProviderSelection {
  if (!value || typeof value !== 'object') return false;
  const selection = value as WellArchitectedProviderSelection;
  return selection.provider_contacted === true && selection.native_qualified === false
    && ([['execution_role_ref', 'role', input.execution_role_arn], ['source_store_ref', 'artifact_store', input.source_bucket]] as const)
      .every(([field, type, id]) => {
        const ref = selection[field];
        return scopedReference(ref, input.connection_ref.tenant_id, input.connection_ref.project_id)
          && ref.source_connection_id === input.connection_ref.source_connection_id
          && ref.record_type === type && ref.record_id === id && /^[a-f0-9]{64}$/.test(ref.record_revision);
      });
}

const actions: Record<string, string> = {
  source_store_unavailable: 'Use the private archive bucket configured for this installed L5 runtime. Ask the runtime owner for its name.',
  environment_selection_required: 'Select and verify this connection’s environment on the Product, then refresh and validate again.',
  source_resources_unqualified: 'Check the separate Well-Architected execution role, its versioned source-read permission, and the bucket’s region, versioning and public-access blocks; then validate again.',
  provider_selection_stale: 'The connection, environment or provider resources changed. Refresh, validate the resources again, and review the new references before saving.',
  provider_selection_required: 'Validate both provider resources and review their returned references before saving.',
  native_generation_active: 'A native review generation is active. Wait for it to finish before changing review inputs.',
  refresh_required: 'The Product profile changed. Refresh its configuration before saving these choices.',
  source_scope_conflict: 'The selected source scope changed. Refresh and review the pinned sources before saving.',
};
export function providerAction(error: unknown): string | undefined {
  if (!error || typeof error !== 'object' || !('detail' in error) || !error.detail || typeof error.detail !== 'object' || !('code' in error.detail)) return;
  return typeof error.detail.code === 'string' ? actions[error.detail.code] : undefined;
}
