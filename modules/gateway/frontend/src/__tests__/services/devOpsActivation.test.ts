import { afterEach, describe, expect, it, vi } from 'vitest';
import { activateInvestigation, activationMatches, bindingMatches, buildActivation, grantResourceConsent, previewResourceConsent,
  readDevOpsConfigurationVersion, resourceSelection, validResourcePreview, verifyResource, type ActivationReceipt, type ResourceCheckReceipt } from '@/services/devOpsActivation';
import { binding, configuration, consent, limits, policy, resources, selection } from '../fixtures/devOpsActivation';

afterEach(() => vi.restoreAllMocks());
const now = Date.parse('2030-01-01T00:01:00Z');
describe('resource verification and activation contracts', () => {
  it('previews resource consent with POST and exact normalized resource selection', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { headers: { ETag: '"config-hash"' } }));
    const body = { binding_id: 'binding', for_control: true as const, control_action: 'devops_resource_verification' as const, resource_selection: selection };
    await previewResourceConsent('product/a', 'project/b', body);
    expect(fetch.mock.calls[0][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/devops/delegation-scope');
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: 'POST', body: JSON.stringify(body) });
  });
  it('reads an exact historical configuration revision without writing or replacing a request key', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { headers: { ETag: '"historical-hash"' } }));
    await readDevOpsConfigurationVersion('product/a', 'project/b', 4);
    expect(fetch.mock.calls[0][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/devops/configuration/versions/4');
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: 'GET' });
    expect(fetch.mock.calls[0][1]?.headers).not.toHaveProperty('Idempotency-Key');
  });
  it('preserves the original grant, verification and activation payloads and revision tokens on retries', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}', { headers: { ETag: '"receipt"' } }));
    const check: ResourceCheckReceipt = { key: 'original_verify_key', etag: '"binding-4"', state: 'pending', body: {
      action: 'check', configuration_revision: 3, binding_id: 'binding', resource_selection: selection } };
    const activation: ActivationReceipt = { key: 'original_activation_key', etag: '"config-hash"', state: 'pending',
      configuration_id: 'config', previous_revision: 3, body: buildActivation(configuration, binding, resources, policy, limits, now) };
    for (const send of [() => grantResourceConsent('product', 'project', consent), () => verifyResource('product', 'project', check),
      () => activateInvestigation('product', 'project', activation)]) {
      await send(); await send();
      expect(fetch.mock.calls.at(-1)).toEqual(fetch.mock.calls.at(-2));
    }
    expect(fetch.mock.calls[2][1]).toMatchObject({ headers: expect.objectContaining({ 'If-Match': check.etag, 'Idempotency-Key': check.key }) });
    expect(fetch.mock.calls[4][1]).toMatchObject({ method: 'PUT', body: JSON.stringify(activation.body),
      headers: expect.objectContaining({ 'If-Match': activation.etag, 'Idempotency-Key': activation.key }) });
  });
  it('derives provider identity from readback and rejects unbounded or foreign resource choices', () => {
    expect(resourceSelection(configuration, binding, selection.role_arn, selection.resource_arn, selection.telemetry_arn)).toEqual(selection);
    for (const invalid of [selection.setup_role_arn, 'arn:aws:iam::000000000000:role/resource']) {
      expect(() => resourceSelection(configuration, binding, invalid, selection.resource_arn, selection.telemetry_arn)).toThrow(/distinct resource role/);
    }
    for (const invalid of [`${selection.resource_arn}:1`, selection.resource_arn.replace('us-east-1', 'us-west-2'), selection.resource_arn.replace('selected', '*')]) {
      expect(() => resourceSelection(configuration, binding, selection.role_arn, invalid, selection.telemetry_arn)).toThrow();
    }
    expect(() => resourceSelection(configuration, { ...binding, ownership: 'adopted' }, selection.role_arn, selection.resource_arn, selection.telemetry_arn)).toThrow();
  });
  it('rejects changed owner, generation, operations, configuration and resource consent scopes', () => {
    const preview = { value: { scope: consent.scope, scope_digest: consent.body.scope_digest, maximum_lifetime_seconds: 86400 }, etag: consent.etag };
    expect(validResourcePreview(preview, configuration, binding, selection)).toBe(true);
    for (const changed of [{ created_by: 'other' }, { binding_generation: 2 }, { operations: [...consent.scope.operations, 'run:submit'] },
      { configuration_revision: 4 }, { resource_selection: { ...selection, role_arn: selection.setup_role_arn } }, { capabilities: ['investigation'] }]) {
      expect(validResourcePreview({ ...preview, value: { ...preview.value, scope: { ...consent.scope, ...changed } } }, configuration, binding, selection)).toBe(false);
    }
    expect(validResourcePreview(preview, { ...configuration, scope: { ...configuration.scope, owner_id: '' } }, binding, selection)).toBe(false);
  });
  it('builds a bounded trigger only from exact API references and preserves unrelated saved settings', () => {
    const saved = { ...configuration, settings: { ...configuration.settings, devops: { ...configuration.settings.devops, retention_days: 77 } } };
    const result = buildActivation(saved, binding, resources, policy, limits, now);
    expect(result).toMatchObject({ enabled: true, capabilities: ['investigation'], classification: 'internal', limits: {
      max_cost: '1.25', max_provider_attempts: 1, concurrency: 1 }, devops: { retention_days: 77, access_policy_ref: policy.policy_ref,
      trigger_policies: [{ kind: 'manual', capability: 'investigation', binding_id: 'binding', enabled: true,
        resource_refs: resources.resource_refs, telemetry_refs: resources.telemetry_refs }] } });
    expect(result.devops.accounts).toEqual([{ account_id: '123456789012', environments: [{ environment_ref: configuration.settings.source_refs[0], resource_refs: resources.resource_refs }] }]);
    expect(configuration.settings.enabled).toBe(false);
  });
  it('records a reason for a new classification and replaces an edited override without duplication', () => {
    const now = Date.parse('2030-01-01T00:01:00Z');
    const initial = buildActivation(configuration, binding, resources, policy, limits, now);
    expect(initial.overrides).toContainEqual({ field: 'classification', value: 'internal', reason: expect.any(String) });
    const other = { field: 'region', value: 'us-east-1', reason: 'Selected environment' };
    const existing = { ...configuration, settings: { ...configuration.settings, classification: 'confidential', overrides: [other,
      { field: 'classification', value: 'confidential', reason: 'Previous selection' }] } };
    const edited = buildActivation(existing, binding, resources, policy, limits, now);
    expect(edited.overrides).toEqual([other, { field: 'classification', value: 'internal', reason: expect.any(String) }]);
    const unchanged = buildActivation(existing, binding, resources, policy, { ...limits, classification: 'confidential' }, now);
    expect(unchanged.overrides).toEqual(existing.settings.overrides);
    expect(existing.settings.overrides[1].value).toBe('confidential');
  });
  it('verifies and activates the exact saved adopted space while preserving its ownership selection', () => {
    const adopted = { ...configuration, settings: { ...configuration.settings, devops: { ...configuration.settings.devops,
      space_mode: 'adopt' as const, existing_space_id: 'native-space' } } };
    const linked = { ...binding, ownership: 'customer_linked', native_resource_ref: {
      ...binding.native_resource_ref!, record_revision: 'native-space' } };
    expect(bindingMatches(linked, adopted)).toBe(true);
    expect(resourceSelection(adopted, linked, selection.role_arn, selection.resource_arn, selection.telemetry_arn)).toEqual(selection);
    expect(validResourcePreview({ value: { scope: consent.scope, scope_digest: consent.body.scope_digest, maximum_lifetime_seconds: 86400 },
      etag: consent.etag }, adopted, linked, selection)).toBe(true);
    const activation = buildActivation(adopted, linked, resources, policy, limits, now);
    expect(activation.devops).toMatchObject({ space_mode: 'adopt', existing_space_id: 'native-space' });
    expect(activation.devops.trigger_policies).toEqual([expect.objectContaining({ binding_id: linked.binding_id, capability: 'investigation', enabled: true })]);
    expect(linked.ownership).toBe('customer_linked');
    for (const changed of [{ ...linked, ownership: 'adp_created' },
      { ...linked, native_resource_ref: { ...linked.native_resource_ref, record_id: 'another-space' } },
      { ...linked, native_resource_ref: { ...linked.native_resource_ref, record_revision: 'stale' } },
      { ...linked, connection_ref: { ...linked.connection_ref, record_revision: 'stale' } }]) {
      expect(bindingMatches(changed, adopted)).toBe(false);
      expect(() => buildActivation(adopted, changed, resources, policy, limits, now)).toThrow();
    }
    expect(bindingMatches(linked, configuration)).toBe(false);
  });
  it('rejects expired observations, stale bindings, changed environments and excessive limits', () => {
    expect(() => buildActivation(configuration, binding, resources, policy, limits, Date.parse(policy.document.resource_observation.expires_at))).toThrow(/fresh verified resources/);
    expect(() => buildActivation(configuration, { ...binding, generation: 3 }, resources, policy, limits, now)).toThrow();
    expect(() => buildActivation(configuration, binding, { ...resources, resource_refs: [{ ...resources.resource_refs[0], project_id: 'other' }] }, policy, limits, now)).toThrow();
    for (const changes of [{ maxCost: 'Infinity' }, { maxCost: '0' }, { deadline: 3601 }, { polls: 61 }]) {
      expect(() => buildActivation(configuration, binding, resources, policy, { ...limits, ...changes }, now)).toThrow(/Set a classification/);
    }
  });
  it('confirms an unknown activation only against the exact next revision and reviewed settings', () => {
    const receipt: ActivationReceipt = { key: 'original_activation_key', etag: '"config-hash"', state: 'pending', configuration_id: 'config',
      previous_revision: 3, body: buildActivation(configuration, binding, resources, policy, limits, now) };
    const result = { value: { ...configuration, revision: 4, hash: 'saved-hash', settings: receipt.body }, etag: '"saved-hash"' };
    expect(activationMatches(result, configuration, receipt)).toBe(true);
    expect(activationMatches({ ...result, value: { ...result.value, revision: 5 } }, configuration, receipt)).toBe(false);
    expect(activationMatches({ ...result, value: { ...result.value, settings: { ...receipt.body, limits: { ...receipt.body.limits, max_cost: '50' } } } }, configuration, receipt)).toBe(false);
    expect(activationMatches({ ...result, value: { ...result.value, scope: { ...configuration.scope, owner_id: 'other' } } }, configuration, receipt)).toBe(false);
  });
});
