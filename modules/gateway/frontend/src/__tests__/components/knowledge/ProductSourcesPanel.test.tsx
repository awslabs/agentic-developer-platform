import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ProductSourcesPanel } from '@/components/knowledge/ProductSourcesPanel';
import { includeProductKnowledgeSource, listProductKnowledgeSources, updateKnowledgeRefresh, type ProductKnowledgeSource } from '@/services/knowledge';

vi.mock('@/services/knowledge', () => ({
  includeProductKnowledgeSource: vi.fn(), listProductKnowledgeSources: vi.fn(), updateKnowledgeRefresh: vi.fn(),
}));

const source: ProductKnowledgeSource = {
  binding_id: 'binding-a', name: 'fixture/service', source_ref: 'https://github.com/fixture/service',
  connection_id: 'connection-a', connection_name: 'fixture', project_name: 'Delivery',
  included: false, eligible: true, can_manage: false,
};

function page(items: ProductKnowledgeSource[], product = 'product-a') {
  return { product_id: product, items, after: null };
}

function renderPanel(product = 'product-a', onChanged = vi.fn()) {
  return render(<MemoryRouter><ProductSourcesPanel key={product} productId={product} onChanged={onChanged} /></MemoryRouter>);
}

describe('Product Context sources', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([source]));
  });

  it('shows the saved connection and explicitly includes a repository with daily refresh', async () => {
    const changed = vi.fn();
    renderPanel('product-a', changed);
    expect(await screen.findByText('Connection: fixture · ADP GitHub App')).toBeInTheDocument();
    expect(screen.getByText('Project: Delivery')).toBeInTheDocument();
    const toggle = screen.getByRole('checkbox', { name: 'Keep updated: fixture/service' });
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(includeProductKnowledgeSource).not.toHaveBeenCalled();
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 1,
      can_manage: true, keep_updated: true, status: 'queued' }]));
    await userEvent.click(screen.getByRole('button', { name: 'Include fixture/service in Context' }));
    expect(includeProductKnowledgeSource).toHaveBeenCalledWith('product-a', 'binding-a', 'https://github.com/fixture/service', true);
    await screen.findByText(/Daily updates enabled/);
    expect(changed).toHaveBeenCalledOnce();
  });

  it('turns off refresh using the saved configuration revision', async () => {
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 3,
      can_manage: true, keep_updated: true, status: 'complete', last_refreshed_at: '2026-01-01T00:00:00Z' }]));
    renderPanel();
    await screen.findByText(/Last successful refresh:/);
    await userEvent.click(screen.getByRole('checkbox', { name: 'Keep updated: fixture/service' }));
    expect(updateKnowledgeRefresh).toHaveBeenCalledWith('asset-a', false, 3);
    expect(includeProductKnowledgeSource).not.toHaveBeenCalled();
  });

  it('allows stopping a revoked source but never enables it', async () => {
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 2,
      eligible: false, can_manage: true, keep_updated: true, error: 'The Product connection was revoked.' }]));
    renderPanel();
    expect(await screen.findByText('The Product connection was revoked.')).toBeInTheDocument();
    const toggle = screen.getByRole('checkbox', { name: 'Keep updated: fixture/service' });
    expect(toggle).toBeEnabled();
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 3,
      eligible: false, can_manage: true, keep_updated: false }]));
    await userEvent.click(toggle);
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeDisabled());
  });

  it('uses server management permissions for existing sources', async () => {
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 1,
      can_manage: false, keep_updated: true }]));
    renderPanel();
    expect(await screen.findByRole('checkbox')).toBeDisabled();
  });

  it('reconnects a changed source through the explicit include API', async () => {
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 1,
      can_manage: true, eligible: false, needs_reconnect: true, keep_updated: true }]));
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Reconnect fixture/service' }));
    expect(includeProductKnowledgeSource).toHaveBeenCalledWith('product-a', 'binding-a', source.source_ref, true);
    expect(updateKnowledgeRefresh).not.toHaveBeenCalled();
  });

  it('restores the saved toggle when its update fails', async () => {
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([{ ...source, included: true, asset_id: 'asset-a', revision: 1,
      can_manage: true, keep_updated: true }]));
    vi.mocked(updateKnowledgeRefresh).mockRejectedValue(new Error('Refresh settings changed. Reload before saving.'));
    renderPanel();
    await userEvent.click(await screen.findByRole('checkbox'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh settings changed. Reload before saving.');
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeChecked());
  });

  it('shows a useful empty state and does not hide provider errors', async () => {
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([]));
    renderPanel();
    expect(await screen.findByText(/No saved GitHub repositories/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Manage GitHub connections' })).toHaveAttribute('href', '/settings/connections');
    vi.mocked(listProductKnowledgeSources).mockRejectedValue({ detail: { message: 'Context database update required.' } });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Context database update required.');
  });

  it('discards a response after leaving the Product', async () => {
    let resolve!: (result: ReturnType<typeof page>) => void;
    vi.mocked(listProductKnowledgeSources).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const view = renderPanel();
    vi.mocked(listProductKnowledgeSources).mockResolvedValue(page([], 'product-b'));
    view.rerender(<MemoryRouter><ProductSourcesPanel key="product-b" productId="product-b" onChanged={vi.fn()} /></MemoryRouter>);
    await screen.findByText(/No saved GitHub repositories/);
    resolve(page([source]));
    await waitFor(() => expect(screen.queryByText('fixture/service')).not.toBeInTheDocument());
    expect(includeProductKnowledgeSource).not.toHaveBeenCalled();
  });
});
