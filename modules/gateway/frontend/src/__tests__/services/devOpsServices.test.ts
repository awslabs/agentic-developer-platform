import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getDevOpsConfiguration, getDevOpsOptions, saveDevOpsConfiguration } from '@/services/l5Services';

const fetchMock = vi.fn();

describe('DevOps configuration transport', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });

  it('reads and saves drafts under the selected Product/project and shared revision contract', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"capabilities":["investigation"],"connections":[],"sources":[],"regions":[],"unavailable_reasons":[]}'))
      .mockResolvedValueOnce(new Response('{"revision":2}', { headers: { ETag: '"two"' } }))
      .mockResolvedValueOnce(new Response('{"revision":3}', { headers: { ETag: '"three"' } }));
    await getDevOpsOptions('product/a', 'project/b');
    const current = await getDevOpsConfiguration('product/a', 'project/b');
    await saveDevOpsConfiguration('product/a', 'project/b', { service_id: 'devops', capabilities: ['investigation'] }, current.etag, 'fixed_receipt_123456');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/devops/configuration/options'),
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/devops/configuration'),
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/devops/configuration'),
    ]);
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'PUT',
      headers: expect.objectContaining({ 'If-Match': '"two"', 'Idempotency-Key': 'fixed_receipt_123456' }),
      body: '{"service_id":"devops","capabilities":["investigation"]}' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
