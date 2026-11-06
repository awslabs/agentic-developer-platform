import { beforeEach, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getToolAccess, getToolsCatalog, getToolsConsentDefault, getToolEntitlements, putToolEntitlement, putToolsConsentDefault,
} from '@/services/toolEntitlements';

vi.mock('@/services/api', () => ({
  apiClient: { get: vi.fn(), put: vi.fn() },
  buildQueryString: (params: Record<string, string | null>) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => { if (value) query.set(key, value); });
    return `?${query}`;
  },
}));

beforeEach(() => vi.clearAllMocks());

it('encodes tenant/team scope, and forwards the displayed revision rather than rewriting it', async () => {
  await getToolsCatalog('tenant/one');
  expect(apiClient.get).toHaveBeenCalledWith('/admin/tools/catalog?tenant_id=tenant%2Fone');
  await getToolEntitlements('tenant/one', 'team + one');
  expect(apiClient.get).toHaveBeenCalledWith('/admin/tools/entitlements?tenant_id=tenant%2Fone&team_id=team+%2B+one');
  await putToolEntitlement({
    tenant_id: 'tenant/one', team_id: 'team + one', tool_name: 'browser.start',
    enabled: false, max_cost_usd: null, expected_revision: 7,
  });
  expect(apiClient.put).toHaveBeenCalledWith('/admin/tools/entitlements', {
    tenant_id: 'tenant/one', team_id: 'team + one', tool_name: 'browser.start',
    enabled: false, max_cost_usd: null, expected_revision: 7,
  });
});


it('encodes the target user and tenant for a read-only access report', async () => {
  await getToolAccess('tenant/one', 'user/one');
  expect(apiClient.get).toHaveBeenCalledWith('/admin/tools/users/user%2Fone/access?tenant_id=tenant%2Fone');
});

it('reads and writes the tenant consent posture, clearing the override with null (#8387)', async () => {
  await getToolsConsentDefault('tenant/one');
  expect(apiClient.get).toHaveBeenCalledWith('/admin/tools/consent-default?tenant_id=tenant%2Fone');
  await putToolsConsentDefault('tenant/one', 'explicit');
  expect(apiClient.put).toHaveBeenCalledWith('/admin/tools/consent-default', { tenant_id: 'tenant/one', consent_default: 'explicit' });
  await putToolsConsentDefault('tenant/one', null);
  expect(apiClient.put).toHaveBeenCalledWith('/admin/tools/consent-default', { tenant_id: 'tenant/one', consent_default: null });
});
