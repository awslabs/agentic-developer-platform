import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { checkFinOpsConfiguration, finOpsBindingReady } from '@/services/finopsReadiness';
import type { FinOpsBinding, FinOpsConfiguration } from '@/services/l5Services';

vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn() } }));
const scope = { tenant_id: 'tenant', product_id: 'product', project_id: 'project', relationship_revision: '1' };
const connection = { tenant_id: 'tenant', project_id: 'project', source_connection_id: 'connection', record_type: 'connection', record_id: 'connection', record_revision: '1' };
const billing = { account_ids: ['111122223333'], view_ref: 'billing-view' };
const configuration = { configuration_id: 'config', revision: 2, scope, settings: { service_id: 'finops', enabled: true,
  connection_ref: connection, finops: { native_agent_ref: 'agent', billing_scope: billing } } } as unknown as FinOpsConfiguration;
const binding = { scope, configuration_id: 'config', state: 'ready', gaps: [], connection_ref: connection,
  native_configuration_revision: 2, native_agent_display_name: 'Example agent', billing_scope: billing,
  native_resource_ref: { ...connection, record_type: 'finops_agent', record_id: 'agent' } } as unknown as FinOpsBinding;

describe('FinOps current readiness', () => {
  beforeEach(() => vi.resetAllMocks());
  it('calls the encoded existing service configuration check endpoint', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ ready: false, gaps: ['native_unqualified'] });
    expect(await checkFinOpsConfiguration('product/one', 'project/one')).toEqual({ ready: false, gaps: ['native_unqualified'] });
    expect(apiClient.post).toHaveBeenCalledWith('/v1/products/product%2Fone/projects/project%2Fone/services/finops/configuration/check');
  });
  it('matches actual billing readback independently of JSON field order', () => {
    expect(finOpsBindingReady(binding, configuration)).toBe(true);
    expect(finOpsBindingReady({ ...binding, connection_ref: Object.fromEntries(Object.entries(connection).reverse()) } as FinOpsBinding, configuration)).toBe(true);
  });
  it.each([
    { state: 'blocked' }, { gaps: ['native_unqualified'] }, { configuration_id: 'other' },
    { native_configuration_revision: 1 }, { native_agent_display_name: undefined },
    { connection_ref: { ...connection, record_revision: '2' } },
    { scope: { ...scope, project_id: 'other' } },
    { native_resource_ref: { ...binding.native_resource_ref, record_id: 'other-agent' } },
    { billing_scope: { ...billing, account_ids: ['444455556666'] } },
    { billing_scope: { ...billing, view_ref: 'other-view' } },
  ])('rejects a binding that no longer matches selected billing scope or configuration (%j)', change => {
    expect(finOpsBindingReady({ ...binding, ...change } as FinOpsBinding, configuration)).toBe(false);
  });
});
