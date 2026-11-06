import { StrictMode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProductStoragePanel } from '@/components/knowledge/ProductStoragePanel';
import * as api from '@/services/contextStorage';
vi.mock('@/services/contextStorage', () => ({ getContextStorage: vi.fn(), configureContextStorage: vi.fn(),
  beginContextUpload: vi.fn(), transferContextFile: vi.fn(), completeContextUpload: vi.fn() }));
const settings: api.ContextStorage = { product_id: 'a', bucket: 'context', prefix: 'tenants/t/products/a/',
  location: 's3://context/tenants/t/products/a/', revision: 2, encryption: 'shared', kms_key_arn: 'shared-key',
  can_configure: false, can_upload: true, max_upload_bytes: 50 * 1024 * 1024, extensions: ['.md'] };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.getContextStorage).mockResolvedValue(settings); });
async function open() { await userEvent.click(screen.getByRole('button', { name: 'Open storage' })); await screen.findByText(settings.location); }
it('loads under StrictMode and respects member permissions', async () => {
  render(<StrictMode><ProductStoragePanel productId="a" onChanged={vi.fn()} /></StrictMode>);
  await open();
  const signal = vi.mocked(api.getContextStorage).mock.calls.at(-1)?.[1];
  expect(signal?.aborted).toBe(false);
  expect(screen.queryByRole('button', { name: 'Save encryption' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Document')).toBeInTheDocument();
});
it('saves with the current revision and shows a conflict without claiming success', async () => {
  vi.mocked(api.getContextStorage).mockResolvedValue({ ...settings, can_configure: true });
  vi.mocked(api.configureContextStorage).mockRejectedValue({ detail: 'Storage settings changed. Refresh before saving.' });
  render(<ProductStoragePanel productId="a" onChanged={vi.fn()} />);
  await open();
  await userEvent.selectOptions(screen.getByRole('combobox'), 'customer');
  await userEvent.type(screen.getByLabelText('Product KMS key ARN'), 'product-key');
  await userEvent.click(screen.getByRole('button', { name: 'Save encryption' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Refresh before saving');
  expect(api.configureContextStorage).toHaveBeenCalledWith('a', { revision: 2, encryption: 'customer', kms_key_arn: 'product-key' }, expect.any(AbortSignal));
  expect(screen.queryByText(/Encryption saved/)).not.toBeInTheDocument();
});
it('retries import completion without uploading a second object', async () => {
  vi.mocked(api.beginContextUpload).mockResolvedValue({ product_id: 'a', upload_id: 'receipt', source_ref: 's3://source', url: 'https://s3.example/upload', headers: {}, expires_in: 300 });
  vi.mocked(api.transferContextFile).mockResolvedValue();
  vi.mocked(api.completeContextUpload).mockRejectedValueOnce(new Error('Retry completion')).mockResolvedValue({ id: 'asset', product_id: 'a', status: 'queued' });
  const changed = vi.fn();
  render(<ProductStoragePanel productId="a" onChanged={changed} />);
  await open();
  await userEvent.upload(screen.getByLabelText('Document'), new File(['guide'], 'guide.md'));
  await userEvent.click(screen.getByRole('button', { name: 'Upload document' }));
  await screen.findByText('Retry completion');
  await userEvent.click(screen.getByRole('button', { name: 'Finish import' }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  expect(api.beginContextUpload).toHaveBeenCalledOnce();
  expect(api.transferContextFile).toHaveBeenCalledOnce();
  expect(api.completeContextUpload).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText('Document')).toHaveValue('');
});
it('cancels outstanding work when navigating away and refuses empty documents', async () => {
  const view = render(<ProductStoragePanel productId="a" onChanged={vi.fn()} />);
  await open();
  await userEvent.upload(screen.getByLabelText('Document'), new File([], 'empty.md'));
  await userEvent.click(screen.getByRole('button', { name: 'Upload document' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('between 1 byte');
  expect(api.beginContextUpload).not.toHaveBeenCalled();
  const signal = vi.mocked(api.getContextStorage).mock.calls[0][1];
  view.unmount();
  expect(signal?.aborted).toBe(true);
});
