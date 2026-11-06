import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ProductCreate from '@/pages/products/ProductCreate';
import ProductOverview from '@/pages/products/ProductOverview';
import { getProductPeople, createProduct, getProduct, updateProduct, type ProductProfile } from '@/services/products';
import { productDraftKey, readProductDraft, writeProductDraft } from '@/pages/products/productDraft';

vi.mock('@/pages/products/ProductSources', () => ({ default: () => null }));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'actor-a', orgId: 'tenant-a' } }) }));
vi.mock('@/services/products', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/products')>(),
  getProductPeople: vi.fn(),
  createProduct: vi.fn(), getProduct: vi.fn(), updateProduct: vi.fn(),
}));

const saved: ProductProfile = { product_id: 'one', project_id: 'two', revision: 1, product: {
  name: 'Planner', purpose: 'Old purpose', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'owner' },
} };

async function renderPage(path: string) {
  const view = render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/products/new" element={<ProductCreate />} />
    <Route path="/products/:productId/projects/:projectId/*" element={<ProductOverview />} />
  </Routes></MemoryRouter>);
  if (path.includes('/projects/')) {
    await userEvent.click(await screen.findByRole('button', { name: 'Edit information' }));
  }
  return view;
}

describe('Product input recovery', () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.resetAllMocks();
    vi.mocked(getProductPeople).mockResolvedValue({ product_id: 'product-1', revision: 1, owner: { id: 'actor-a', name: 'Owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    vi.mocked(getProduct).mockResolvedValue(saved);
  });

  it('identifies invalid fields, focuses the first error, and does not submit', async () => {
    const user = userEvent.setup();
    await renderPage('/products/new');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Check the highlighted fields');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('textbox', { name: 'Intended users' })).toHaveAccessibleDescription(/distinct intended users/i);
    expect(createProduct).not.toHaveBeenCalled();
  });

  it('announces the server field rejection without losing typed input', async () => {
    const user = userEvent.setup();
    vi.mocked(createProduct).mockRejectedValue({ status: 422, detail: { code: 'validation_error', fields: [{ field: 'body.intended_users', reason: 'value_error' }] } });
    await renderPage('/products/new');
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Planner');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'A purpose');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Check the highlighted fields');
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Intended users' })).toHaveFocus());
    expect(screen.getByRole('textbox', { name: 'Intended users' })).toHaveValue('Maintainers');
  });

  it('explains missing team membership and keeps a rejected creation editable', async () => {
    const user = userEvent.setup();
    vi.mocked(createProduct).mockRejectedValue({ status: 409, detail: { code: 'team_assignment_required' } });
    await renderPage('/products/new');
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Planner');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'A purpose');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a primary ADP team');
    expect(screen.getByRole('button', { name: 'Save product' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: 'Name' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Planner');
    expect(screen.queryByRole('button', { name: 'Check save status' })).not.toBeInTheDocument();
    expect(readProductDraft(productDraftKey('actor-a', 'tenant-a', 'new'))).toEqual({
      fields: { name: 'Planner', purpose: 'A purpose', users: 'Maintainers' },
    });
    expect(getProduct).not.toHaveBeenCalled();
  });

  it('keeps offline creation input across a remount in the same signed-in session', async () => {
    const user = userEvent.setup();
    vi.mocked(createProduct).mockRejectedValue(new TypeError('Network offline'));
    const view = await renderPage('/products/new');
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Offline draft');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'A purpose');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't confirm/i);
    view.unmount();
    await renderPage('/products/new');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Offline draft');
    expect(screen.getByRole('textbox', { name: 'Purpose' })).toHaveValue('A purpose');
  });

  it('does not expose one signed-in account draft to another account or organization', () => {
    const firstKey = productDraftKey('actor-a', 'tenant-a', 'new');
    writeProductDraft(firstKey, { name: 'Private draft', purpose: 'A purpose', users: 'Maintainers' });
    expect(readProductDraft(productDraftKey('actor-b', 'tenant-a', 'new'))).toBeNull();
    expect(readProductDraft(productDraftKey('actor-a', 'tenant-b', 'new'))).toBeNull();
    expect(readProductDraft(firstKey)?.fields.name).toBe('Private draft');
  });

  it('restores an offline edit and compares it with the latest server revision after remount', async () => {
    const user = userEvent.setup();
    const view = await renderPage('/products/one/projects/two');
    const field = await screen.findByRole('textbox', { name: 'Purpose' });
    await user.clear(field);
    await user.type(field, 'Offline edit');
    view.unmount();
    vi.mocked(getProduct).mockResolvedValue({ ...saved, revision: 2, product: { ...saved.product, purpose: 'Other client edit' } });
    await renderPage('/products/one/projects/two');
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Purpose' })).toHaveValue('Offline edit'));
    expect(screen.getByText(/Another client saved revision 2/i)).toBeInTheDocument();
    expect(updateProduct).not.toHaveBeenCalled();
  });
});
