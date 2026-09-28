import { beforeEach, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { loadIdentityHierarchy, loadServiceIdentityPage } from '@/services/organizationServiceIdentities';
vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));
beforeEach(() => vi.resetAllMocks());

it('uses org-scoped reads and retains each source pagination contract', async () => {
  vi.mocked(apiClient.get).mockResolvedValueOnce({ items: [{ client_id: 'c', name: 'worker', org_id: 'a/b', client_secret: 'must-not-copy' }], has_more: true });
  const result = await loadServiceIdentityPage('a/b', 'cognito');
  expect(apiClient.get).toHaveBeenLastCalledWith('/admin/agents?page_size=50&org_id=a%2Fb&page=1');
  expect(result.next).toBe('2');
  expect(result.items[0]).not.toHaveProperty('client_secret');
  vi.mocked(apiClient.get).mockResolvedValueOnce({ items: [], last_key: 'next-key' });
  expect((await loadServiceIdentityPage('a/b', 'iam', 'cursor+value')).next).toBe('next-key');
  expect(apiClient.get).toHaveBeenLastCalledWith('/admin/registry/agents?page_size=50&org_id=a%2Fb&last_key=cursor%2Bvalue');
  vi.mocked(apiClient.get).mockResolvedValueOnce({ items: [], has_more: false });
  expect((await loadServiceIdentityPage('a/b', 'legacy', '2')).next).toBeNull();
  expect(apiClient.get).toHaveBeenLastCalledWith('/admin/organizations/a%2Fb/service-accounts?page_size=50&page=2');
});

it('rejects an account belonging to a different organization', async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ items: [{ client_id: 'foreign', org_id: 'other' }] });
  await expect(loadServiceIdentityPage('mine', 'cognito')).rejects.toThrow('Unexpected organization');
});

it('loads hierarchy names beyond the first metadata page', async () => {
  vi.mocked(apiClient.get).mockImplementation(async path => path.includes('/departments?')
    ? path.includes('page=1&') ? { items: [{ id: 'first', name: 'First' }], has_more: true } : { items: [{ id: 'last', name: 'Last' }], has_more: false }
    : { items: [{ id: 'team', name: 'Cyber', department_id: 'last' }], has_more: false });
  const result = await loadIdentityHierarchy('org');
  expect(result.departments.last).toBe('Last');
  expect(result.teams.team).toEqual({ name: 'Cyber', departmentId: 'last' });
});
