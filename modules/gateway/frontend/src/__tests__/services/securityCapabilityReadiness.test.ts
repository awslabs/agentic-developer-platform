import { describe, expect, it } from 'vitest';
import { securityCapabilityReady } from '@/services/securityCapabilityReadiness';

const now = Date.parse('2026-11-01T10:00:00Z');
const reference = { tenant_id: 'tenant', project_id: 'project', source_connection_id: 'aws-connection',
  record_type: 'connection', record_id: 'aws-connection', record_revision: '1' };
const registration = { kind: 'code_review_source_registration', observed_at: '2026-11-01T09:59:00Z',
  registration: { bucket: 'example-security-sources', service_role: 'arn:aws:iam::111122223333:role/security' } };
const model = { kind: 'threat_model', observed_at: '2026-11-01T09:59:00Z',
  native_model_ref: { ...reference, record_type: 'threat_model', record_id: 'model', record_revision: '2' } };
const binding: Parameters<typeof securityCapabilityReady>[0] = {
  scope: { tenant_id: 'tenant', product_id: 'product', project_id: 'project', relationship_revision: '1' },
  binding_id: 'binding', configuration_id: 'config', revision: 1, generation: 1, state: 'ready', ownership: 'adp_created',
  connection_ref: reference, provider_identity: { account_id: '111122223333' },
  native_resource_ref: { ...reference, record_type: 'security_space', record_id: 'space' }, gaps: [], readiness: [registration, model],
};

describe('Security capability observations', () => {
  it('requires the native model only for threat modeling', () => {
    expect(securityCapabilityReady(binding, 'threat_model', now)).toBe(true);
    expect(securityCapabilityReady({ ...binding, readiness: [registration] }, 'code_review', now)).toBe(true);
    expect(securityCapabilityReady({ ...binding, readiness: [registration] }, 'threat_model', now)).toBe(false);
    expect(securityCapabilityReady(binding, 'pentesting', now)).toBe(false);
  });

  it.each(['blocked', 'revoked', 'outcome_unknown'])('rejects a %s binding even with prior observations', state => {
    expect(securityCapabilityReady({ ...binding, state }, 'code_review', now)).toBe(false);
  });

  it.each(['2026-11-01T09:45:00Z', '2026-11-01T10:00:01Z', '2026-11-01T09:59:00', 'invalid'])('rejects stale or unverifiable registration at %s', observed_at => {
    expect(securityCapabilityReady({ ...binding, readiness: [{ ...registration, observed_at }, model] }, 'threat_model', now)).toBe(false);
  });

  it('rejects revoked observations, missing identity and another account’s role', () => {
    expect(securityCapabilityReady({ ...binding, readiness: [{ ...registration, state: 'revoked' }, model] }, 'threat_model', now)).toBe(false);
    expect(securityCapabilityReady({ ...binding, provider_identity: undefined }, 'code_review', now)).toBe(false);
    expect(securityCapabilityReady({ ...binding, provider_identity: { account_id: '444455556666' } }, 'code_review', now)).toBe(false);
  });

  it('does not interpret an old capability matrix as Security provider evidence', () => {
    expect(securityCapabilityReady({ ...binding, readiness: [{ capability: 'threat_model', state: 'ready', reason_codes: [] }] }, 'threat_model', now)).toBe(false);
  });

  it('refuses unknown, stale or foreign native models while keeping verified code review independent', () => {
    for (const pending of [
      { kind: 'threat_model_outcome_unknown' },
      { kind: 'threat_model_selection_required' },
      { ...model, observed_at: '2026-11-01T09:40:00Z' },
      { ...model, native_model_ref: { ...model.native_model_ref, project_id: 'foreign-project' } },
    ]) {
      expect(securityCapabilityReady({ ...binding, readiness: [registration, pending] }, 'threat_model', now)).toBe(false);
      expect(securityCapabilityReady({ ...binding, readiness: [registration, pending] }, 'code_review', now)).toBe(true);
    }
  });
});
