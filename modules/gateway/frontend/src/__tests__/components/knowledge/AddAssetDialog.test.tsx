import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AddAssetDialog } from '@/components/knowledge/AddAssetDialog';
import { getAccessibleRepos, createAsset } from '@/services/knowledge';

vi.mock('@/services/knowledge', () => ({ getAccessibleRepos: vi.fn(), createAsset: vi.fn(), bulkPreview: vi.fn(), bulkCommit: vi.fn() }));

function renderDialog(productId?: string) {
  return render(<AddAssetDialog isOpen onClose={vi.fn()} onAssetAdded={vi.fn()}
    scope={productId ? 'product' : 'personal'} productId={productId} />);
}

describe('Add information', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getAccessibleRepos).mockResolvedValue({ repos: [], total: 0, page: 1, hasMore: false });
    vi.mocked(createAsset).mockResolvedValue({ id: 'asset-1', status: 'queued' } as Awaited<ReturnType<typeof createAsset>>);
  });

  it('uses one link field without source or sharing tabs', () => {
    renderDialog('product-one');
    expect(screen.getByLabelText('Link')).toBeInTheDocument();
    expect(screen.getByText('Your Product team and platform admins can access information added here.')).toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/scope/i)).not.toBeInTheDocument();
    expect(getAccessibleRepos).not.toHaveBeenCalled();
  });

  it('opens the existing bulk import inside Add information with Product sharing', async () => {
    renderDialog('product-one');
    await userEvent.click(screen.getByRole('button', { name: 'Import a list of links' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Import a list of links' })).toBeInTheDocument();
    expect(screen.getByText('Your Product team and platform admins can access information added here.')).toBeInTheDocument();
  });

  it.each([
    ['https://github.com/acme/service', 'repo', 'acme/service'],
    ['https://docs.example.com/manual', 'url', undefined],
    ['s3://example-documents/manual.pdf', 'doc', undefined],
  ])('infers %s and registers it in this Product', async (source, type, displayName) => {
    renderDialog('product-one');
    await userEvent.type(screen.getByLabelText('Link'), source);
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    expect(createAsset).toHaveBeenCalledWith({ asset_type: type, source_ref: source,
      display_name: displayName, scope: 'product', product_id: 'product-one' });
  });

  it('retains personal ownership for a public repository', async () => {
    renderDialog();
    await userEvent.type(screen.getByLabelText('Link'), 'https://github.com/acme/public.git/');
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    expect(createAsset).toHaveBeenCalledWith({ asset_type: 'repo', source_ref: 'https://github.com/acme/public',
      display_name: 'acme/public', scope: 'personal' });
  });

  it('adds an optional name without changing sharing', async () => {
    renderDialog();
    await userEvent.type(screen.getByLabelText('Link'), 's3://example-documents/manual.pdf');
    await userEvent.type(screen.getByLabelText('Name (optional)'), 'Technical manual');
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    expect(createAsset).toHaveBeenCalledWith({ asset_type: 'doc', source_ref: 's3://example-documents/manual.pdf',
      display_name: 'Technical manual', scope: 'personal' });
  });

  it.each(['', 'doc://manual', 's3://bucket', 'javascript:alert(1)', 'https://token@example.com/private'])('rejects an unusable source: %s', async (source) => {
    renderDialog();
    if (source) await userEvent.type(screen.getByLabelText('Link'), source);
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a web link, GitHub repository URL, or S3 document URI.');
    expect(createAsset).not.toHaveBeenCalled();
  });

  it('uses the existing authenticated repository picker', async () => {
    vi.mocked(getAccessibleRepos).mockResolvedValue({ repos: [{ fullName: 'acme/private', url: 'https://github.com/acme/private', private: true }], total: 1, page: 1, hasMore: false });
    renderDialog('product-one');
    await userEvent.click(screen.getByRole('button', { name: 'Choose a connected repository' }));
    await userEvent.click(await screen.findByRole('button', { name: /acme\/private/ }));
    expect(screen.getByLabelText('Link')).toHaveValue('https://github.com/acme/private');
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    expect(createAsset).toHaveBeenCalledWith({ asset_type: 'repo', source_ref: 'https://github.com/acme/private', display_name: 'acme/private', scope: 'product', product_id: 'product-one' });
  });

  it('shows server authorization errors without losing the draft', async () => {
    vi.mocked(createAsset).mockRejectedValue({ detail: { message: 'You cannot access this document.' } });
    renderDialog('product-one');
    await userEvent.type(screen.getByLabelText('Link'), 's3://example-documents/manual.pdf');
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('You cannot access this document.');
    expect(screen.getByLabelText('Link')).toHaveValue('s3://example-documents/manual.pdf');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add information' })).toBeEnabled());
  });
});
