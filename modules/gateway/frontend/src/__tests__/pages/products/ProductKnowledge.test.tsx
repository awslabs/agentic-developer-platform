import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ProductOverview from '@/pages/products/ProductOverview';
import { getProduct, listProductScopes } from '@/services/products';
import { listAssets, createAsset, listProductKnowledgeSources } from '@/services/knowledge';

vi.mock('@/pages/products/ProductSources', () => ({ default: () => null }));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'member-one', orgId: 'org-one' } }) }));
vi.mock('@/contexts/ToastContext', () => ({ useToast: () => ({ success: vi.fn(), error: vi.fn() }) }));
vi.mock('@/pages/products/ProductPeople', () => ({ default: () => null }));
vi.mock('@/pages/products/ProductGitlabHome', () => ({ default: () => null }));
vi.mock('@/services/products', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/products')>(),
  getProduct: vi.fn(), listProductScopes: vi.fn(),
}));
vi.mock('@/services/knowledge', () => ({ listAssets: vi.fn(), createAsset: vi.fn(),
  listProductKnowledgeSources: vi.fn().mockResolvedValue({ product_id: 'product-one', items: [], after: null }),
}));

function renderProduct(path = '/products/product-one/projects/project-a') {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/products/:productId/projects/:projectId/*" element={<ProductOverview />} />
  </Routes></MemoryRouter>);
}

describe('Product Context tab', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    sessionStorage.clear();
    vi.mocked(listProductScopes).mockResolvedValue(['project-a', 'project-b']);
    vi.mocked(getProduct).mockImplementation(async (productId, projectId) => ({
      product_id: productId, project_id: projectId, revision: 1,
      product: { name: 'Planner', purpose: 'Plan delivery', intended_users: ['Teams'], owner_ref: { kind: 'user', id: 'owner-one' } },
    }));
    vi.mocked(listAssets).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20, hasMore: false, quota: null });
    vi.mocked(listProductKnowledgeSources).mockResolvedValue({ product_id: 'product-one', items: [], after: null });
    vi.mocked(createAsset).mockResolvedValue({ id: 'new-information' } as Awaited<ReturnType<typeof createAsset>>);
  });

  it('loads one shared Product collection only when the tab opens', async () => {
    renderProduct();
    const tab = await screen.findByRole('button', { name: 'Knowledge' });
    expect(listAssets).not.toHaveBeenCalled();
    await userEvent.click(tab);
    await screen.findByText('No information yet. Add a link to get started.');
    expect(listAssets).toHaveBeenCalledWith({ scope: 'product', productId: 'product-one', search: undefined, page: 1, pageSize: 20 });
    expect(screen.queryByRole('complementary', { name: 'Product summary' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
  });

  it('retains Product Context when switching delivery Projects and reopening the workspace', async () => {
    const view = renderProduct();
    await userEvent.click(await screen.findByRole('button', { name: 'Knowledge' }));
    await screen.findByRole('button', { name: 'Add information' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Project' }), 'project-b');
    await waitFor(() => expect(getProduct).toHaveBeenCalledWith('product-one', 'project-b'));
    expect(await screen.findByRole('button', { name: 'Knowledge' })).toHaveAttribute('aria-current', 'page');
    await screen.findByRole('button', { name: 'Add information' });
    expect(vi.mocked(listAssets).mock.calls.every(([params]) => params?.scope === 'product' && params.productId === 'product-one' && !('project_id' in params))).toBe(true);
    view.unmount();
    renderProduct('/products/product-one/projects/project-b/knowledge');
    expect(await screen.findByRole('button', { name: 'Knowledge' })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByRole('button', { name: 'Add information' })).toBeInTheDocument();
  });

  it('creates information for the Product from its tab', async () => {
    renderProduct();
    await userEvent.click(await screen.findByRole('button', { name: 'Knowledge' }));
    await userEvent.click(screen.getByRole('button', { name: 'Add information' }));
    await userEvent.type(screen.getByLabelText('Link'), 'https://docs.example.com/overview');
    const buttons = screen.getAllByRole('button', { name: 'Add information' });
    await userEvent.click(buttons[buttons.length - 1]);
    expect(createAsset).toHaveBeenCalledWith({ asset_type: 'url', source_ref: 'https://docs.example.com/overview', display_name: undefined, scope: 'product', product_id: 'product-one' });
  });
});
