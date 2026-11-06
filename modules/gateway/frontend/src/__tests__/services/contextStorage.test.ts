import { afterEach, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { contextDocumentDownload, getContextStorage, transferContextFile, type ContextUpload } from '@/services/contextStorage';
vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }));
afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals(); });
it('refuses storage responses naming another Product or document', async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ product_id: 'other' });
  await expect(getContextStorage('a')).rejects.toThrow('confirm storage');
  vi.mocked(apiClient.post).mockResolvedValue({ product_id: 'a', asset_id: 'other', url: 'https://example.test' });
  await expect(contextDocumentDownload('a', 'asset')).rejects.toThrow('confirm the requested document');
});
it('sends only transfer headers and never ADP cookies or bearer credentials to S3', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true }); vi.stubGlobal('fetch', fetcher);
  const receipt: ContextUpload = { product_id: 'a', upload_id: 'u', source_ref: 's3://context/key', url: 'https://s3.example/key',
    headers: { 'Content-Type': 'text/markdown', 'x-amz-checksum-sha256': 'checksum' }, expires_in: 300 };
  const file = new File(['hi'], 'guide.md'); const signal = new AbortController().signal;
  await transferContextFile(receipt, file, signal);
  expect(fetcher).toHaveBeenCalledWith(receipt.url, { method: 'PUT', body: file, headers: receipt.headers, credentials: 'omit', redirect: 'error', signal });
  expect(apiClient.post).not.toHaveBeenCalled();
});
