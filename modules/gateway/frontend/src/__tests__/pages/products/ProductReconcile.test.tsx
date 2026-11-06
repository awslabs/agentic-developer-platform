import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ProductCreate from '@/pages/products/ProductCreate';
import ProductOverview from '@/pages/products/ProductOverview';
import { getProductPeople, createProduct, getProduct, updateProduct, type ProductProfile } from '@/services/products';

vi.mock('@/pages/products/ProductSources', () => ({ default: () => null }));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'actor-a', orgId: 'tenant-a' } }) }));
vi.mock('@/services/products', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/products')>(),
  getProductPeople: vi.fn(),
  createProduct: vi.fn(), getProduct: vi.fn(), updateProduct: vi.fn(),
}));

const first: ProductProfile = { product_id: 'one', project_id: 'two', revision: 1, product: {
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

async function fillCreate() {
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Planner');
  await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'Old purpose');
  await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
  await user.click(screen.getByRole('button', { name: 'Save product' }));
}

describe('uncertain Product saves', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getProductPeople).mockResolvedValue({ product_id: 'product-1', revision: 1, owner: { id: 'actor-a', name: 'Owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    sessionStorage.clear();
  });

  it('replays the original create receipt after a dropped response, including across remount', async () => {
    const user = userEvent.setup();
    vi.mocked(createProduct).mockRejectedValueOnce(new TypeError('Response dropped')).mockResolvedValue(first);
    vi.mocked(getProduct).mockResolvedValue(first);
    const page = await renderPage('/products/new');
    await fillCreate();
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't confirm/i);
    expect(screen.getByRole('button', { name: 'Save product' })).toBeDisabled();
    const original = vi.mocked(createProduct).mock.calls[0];
    page.unmount();
    await renderPage('/products/new');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Planner');
    await user.click(screen.getByRole('button', { name: 'Check save status' }));
    await waitFor(() => expect(createProduct).toHaveBeenCalledTimes(2));
    expect(vi.mocked(createProduct).mock.calls[1]).toEqual(original);
    expect(await screen.findByText('Saved revision 1')).toBeInTheDocument();
  });

  it('uses readback rather than repeating the create POST when only the read response drops', async () => {
    const user = userEvent.setup();
    vi.mocked(createProduct).mockResolvedValue(first);
    vi.mocked(getProduct).mockRejectedValueOnce(new TypeError('Read response dropped')).mockResolvedValue(first);
    await renderPage('/products/new');
    await fillCreate();
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't confirm/i);
    await user.click(screen.getByRole('button', { name: 'Check save status' }));
    expect(await screen.findByText('Saved revision 1')).toBeInTheDocument();
    expect(createProduct).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getProduct).mock.calls.slice(0, 2)).toEqual([['one', 'two'], ['one', 'two']]);
  });

  it('replays the same edit receipt and revision without claiming an uncertain write succeeded', async () => {
    const user = userEvent.setup();
    const updated: ProductProfile = { ...first, revision: 2, product: { ...first.product, purpose: 'My edit' } };
    vi.mocked(getProduct).mockResolvedValueOnce(first).mockResolvedValue(updated);
    vi.mocked(updateProduct).mockRejectedValueOnce(new TypeError('Response dropped')).mockResolvedValue(updated);
    await renderPage('/products/one/projects/two');
    const field = await screen.findByRole('textbox', { name: 'Purpose' });
    await user.clear(field);
    await user.type(field, 'My edit');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText(/couldn't confirm/i)).toBeInTheDocument();
    expect(field).toHaveValue('My edit');
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    expect(screen.queryByRole('status', { name: 'Saved revision 2' })).not.toBeInTheDocument();
    const original = vi.mocked(updateProduct).mock.calls[0];
    await user.click(screen.getByRole('button', { name: 'Check save status' }));
    await waitFor(() => expect(updateProduct).toHaveBeenCalledTimes(2));
    expect(vi.mocked(updateProduct).mock.calls[1]).toEqual(original);
    expect(await screen.findByRole('status')).toHaveTextContent('Saved revision 2');
  });
  it('ui_input_recovery: focuses invalid fields and preserves an offline save for single-receipt reconciliation', async () => {
    const user = userEvent.setup();
    vi.mocked(createProduct).mockRejectedValueOnce(new TypeError('Offline')).mockResolvedValue(first);
    vi.mocked(getProduct).mockResolvedValue(first);
    const view = await renderPage('/products/new');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    expect(screen.getByRole('alert')).toHaveTextContent('Check the highlighted fields');
    expect(createProduct).not.toHaveBeenCalled();
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Planner');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'Old purpose');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't confirm/i);
    expect(screen.getByRole('textbox', { name: 'Purpose' })).toHaveValue('Old purpose');
    const original = vi.mocked(createProduct).mock.calls[0];
    view.unmount();
    await renderPage('/products/new');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('Planner');
    await user.click(screen.getByRole('button', { name: 'Check save status' }));
    expect(await screen.findByRole('heading', { name: 'Planner' })).toBeInTheDocument();
    expect(createProduct).toHaveBeenCalledTimes(2);
    expect(vi.mocked(createProduct).mock.calls[1]).toEqual(original);
    expect(getProduct).toHaveBeenCalledWith('one', 'two');
  });

});
