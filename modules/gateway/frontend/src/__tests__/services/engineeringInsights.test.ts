import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { getBusinessContextEvidence } from '@/services/engineeringInsights';
vi.mock('@/services/api', async original => ({ ...await original<typeof import('@/services/api')>(), apiClient: { get: vi.fn() } }));
const asOf = '2026-11-02T12:00:00.000Z';
const snapshot = { schema_version: 'results/1.0', view: 'business_context', product_id: 'p', project_id: 'j', as_of: asOf, dataset_version: 'one', mapping_versions: ['mapping'], coverage: 'complete', work_count: 4, cost_by_currency: { USD: '5.00' }, contexts: [], next_cursor: null };
beforeEach(() => vi.resetAllMocks());
describe('Engineering Insights reader', () => {
  it('pins product, project, timestamp and pagination in each read', async () => {
    vi.mocked(apiClient.get).mockResolvedValue(snapshot);
    expect(await getBusinessContextEvidence('p', 'j', asOf, 'cursor-one')).toEqual(snapshot);
    const url = new URL(vi.mocked(apiClient.get).mock.calls[0][0], 'http://localhost');
    expect(url.pathname).toBe('/engineering-insights/v1/projects/j/results');
    expect(Object.fromEntries(url.searchParams)).toEqual({ view: 'business_context', product_id: 'p', as_of: asOf, limit: '25', cursor: 'cursor-one' });
  });
  it.each([{ product_id: 'foreign' }, { project_id: 'foreign' }, { as_of: 'old' }, { coverage: 'partial' }, { work_count: null }, { contexts: [{}] }, { cost_by_currency: { USD: 'unknown' } }, { schema_version: 'unknown' }])('rejects wrong scope or malformed evidence: %j', async patch => {
    vi.mocked(apiClient.get).mockResolvedValue({ ...snapshot, ...patch });
    await expect(getBusinessContextEvidence('p', 'j', asOf)).rejects.toThrow('Evidence does not match');
  });
});
