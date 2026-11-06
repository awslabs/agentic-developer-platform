import type { SecurityBinding, SecurityReadinessEntry } from './l5Services';

export const isSecurityCapability = (capability: unknown): capability is 'threat_model' | 'code_review' =>
  capability === 'threat_model' || capability === 'code_review';

type ObservedBinding = SecurityBinding & { provider_identity?: { account_id?: string } };
const observedRecently = (entry: SecurityReadinessEntry, now: number) => {
  const value = entry.observed_at;
  if (!value || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return false;
  const age = now - Date.parse(value);
  return Number.isFinite(age) && age >= 0 && age < 15 * 60 * 1000;
};

/** Project the Security adapter's observations, not another service's status
 * matrix. This only enables requesting admission; the server verifies again. */
export function securityCapabilityReady(binding: ObservedBinding, capability: string, now = Date.now()): boolean {
  if (binding.state !== 'ready' || !Array.isArray(binding.gaps) || binding.gaps.length
    || !Array.isArray(binding.readiness) || binding.readiness.length < 1 || binding.readiness.length > 2) return false;
  const [registrationEntry, model] = binding.readiness;
  if (!registrationEntry || registrationEntry.kind !== 'code_review_source_registration'
    || !observedRecently(registrationEntry, now)) return false;
  if (binding.readiness.some(entry => !entry || (entry.state !== undefined && entry.state !== 'ready')
    || (entry.reason_codes !== undefined && (!Array.isArray(entry.reason_codes) || entry.reason_codes.length > 0)))) return false;
  if (model && !['threat_model', 'threat_model_outcome_unknown', 'threat_model_selection_required'].includes(model.kind ?? '')) return false;
  const registration = registrationEntry.registration;
  const account = binding.provider_identity?.account_id;
  if (!registration || Object.keys(registration).sort().join(',') !== 'bucket,service_role'
    || !account || !/^\d{12}$/.test(account)
    || typeof registration.bucket !== 'string' || !/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(registration.bucket)
    || registration.bucket.includes('..') || typeof registration.service_role !== 'string'
    || !new RegExp(`^arn:aws:iam::${account}:role/[A-Za-z0-9_+=,.@/-]+$`).test(registration.service_role)) return false;
  if (capability === 'code_review') return true;
  if (capability !== 'threat_model' || !model || model.kind !== 'threat_model' || !observedRecently(model, now)) return false;
  const reference = model.native_model_ref;
  const space = binding.native_resource_ref;
  return Boolean(reference && space && reference.record_type === 'threat_model' && reference.record_id && reference.record_revision
    && reference.tenant_id === binding.scope.tenant_id && reference.project_id === binding.scope.project_id
    && reference.source_connection_id === binding.connection_ref.source_connection_id
    && ['tenant_id', 'project_id', 'source_connection_id'].every(field =>
      reference[field as keyof typeof reference] && reference[field as keyof typeof reference] === space[field as keyof typeof space]));
}
