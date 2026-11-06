import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ProductOverview from '@/pages/products/ProductOverview';
import { getSharedProductProfile, getProductPeople, getProduct, listProductScopes, listProductSources, listSourceBindings, updateProduct, type ProductProfile } from '@/services/products';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'actor-a', orgId: 'tenant-a' } }) }));
vi.mock('@/pages/products/ProductGitlabHome', () => ({ default: () => null }));
vi.mock('@/pages/products/JiraContextSources', () => ({ default: () => null }));

vi.mock('@/services/products', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/products')>(),
  getSharedProductProfile: vi.fn(), getProductPeople: vi.fn(),
  getProduct: vi.fn(), listProductScopes: vi.fn(), updateProduct: vi.fn(), listProductSources: vi.fn(), listSourceBindings: vi.fn(),
}));

const first: ProductProfile = { product_id: 'one', project_id: 'two', revision: 1, product: {
  name: 'Planner', purpose: 'Old purpose', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'owner' },
} };
const latest: ProductProfile = { ...first, revision: 2, product: { ...first.product, purpose: 'Updated from CLI' } };

async function renderOverview() {
  const view = render(<MemoryRouter initialEntries={['/products/one/projects/two']}><Routes>
    <Route path="/products/:productId/projects/:projectId/*" element={<ProductOverview />} />
    <Route path="/products/:productId/projects/:projectId/services" element={<ProductOverview />} />
    <Route path="/products/:productId/projects/:projectId/services/security" element={<ProductOverview />} />
  </Routes></MemoryRouter>);
  await userEvent.click(await screen.findByRole('button', { name: 'Edit information' }));
  return view;
}

async function refresh() {
  window.dispatchEvent(new Event('focus'));
  await waitFor(() => expect(getProduct).toHaveBeenCalledTimes(2));
}

describe('revision-aware information editing', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listProductScopes).mockResolvedValue(['two']);
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [] });
    vi.mocked(getProductPeople).mockResolvedValue({ product_id: 'product-1', revision: 1, owner: { id: 'actor-a', name: 'Owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    vi.mocked(getSharedProductProfile).mockResolvedValue({ product_id: 'one', revision: 1, base_project_id: 'two', can_edit: true, resources: [], environments: [], not_applicable: [], knowledge_count: 0, completion: { completed: 1, applicable: 5, percentage: 20, sections: [] } });
    sessionStorage.clear();
    vi.mocked(getProduct).mockResolvedValueOnce(first).mockResolvedValue(latest);
  });

  it('shows deferred Context and L5 directly from the scoped API while keeping edits available', async () => {
    const availability = {
      context: { status: 'unavailable' as const, reason: 'integration_deferred' as const, message: 'Context integration is deferred; your saved Product remains available.' },
      l5: { status: 'unavailable' as const, reason: 'integration_deferred' as const, message: 'L5 integration is deferred; your saved Product remains available.' },
    };
    vi.mocked(getProduct).mockReset().mockResolvedValue({ ...first, peer_availability: availability });
    await renderOverview();
    await userEvent.click(screen.getByText('Product profile sharing', { selector: 'summary' }));
    const section = screen.getByText('Product profile sharing', { selector: 'summary' }).closest('details')!;
    expect(within(section).getByText('Product Context: unavailable')).toBeInTheDocument();
    expect(within(section).getByText('Service profile sharing: unavailable')).toBeInTheDocument();
    expect(within(section).getByText(availability.context.message)).toBeInTheDocument();
    expect(within(section).getByText(availability.l5.message)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Agents' }));
    expect(await screen.findByRole('region', { name: 'Product agents' })).toBeInTheDocument();
    expect(within(section).getByText(availability.l5.message)).not.toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Profile' }));
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeInTheDocument();
  });

  it('refreshes an untouched form when another client saves', async () => {
    await renderOverview();
    expect(await screen.findByRole('textbox', { name: 'Purpose' })).toHaveValue('Old purpose');
    await refresh();
    expect(await screen.findByRole('textbox', { name: 'Purpose' })).toHaveValue('Updated from CLI');
    expect(screen.queryByText(/Review conflicting changes/i)).not.toBeInTheDocument();
  });

  it('keeps dirty fields and requires explicit review before applying them on a new revision', async () => {
    const user = userEvent.setup();
    const saved = { ...latest, revision: 3, product: { ...latest.product, purpose: 'My edit' } };
    vi.mocked(updateProduct).mockResolvedValue(saved);
    vi.mocked(getProduct).mockReset().mockResolvedValueOnce(first).mockResolvedValueOnce(latest).mockResolvedValue(saved);
    await renderOverview();
    const field = await screen.findByRole('textbox', { name: 'Purpose' });
    await user.clear(field);
    await user.type(field, 'My edit');
    await refresh();
    expect(field).toHaveValue('My edit');
    expect(await screen.findByText(/Another client saved revision 2/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    expect(updateProduct).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Replace saved fields with my edits' }));
    await waitFor(() => expect(updateProduct).toHaveBeenCalledWith('one', 'two', 2, {
      name: 'Planner', purpose: 'My edit', intended_users: ['Maintainers'],
    }, expect.stringMatching(/^[a-f0-9]{32}$/)));
    expect(await screen.findByRole('status', { name: '' })).toHaveTextContent('Saved revision 3');
  });

  it('never shows saved after 412 and keeps the draft for review', async () => {
    const user = userEvent.setup();
    vi.mocked(updateProduct).mockRejectedValue({ status: 412, detail: { code: 'stale_revision' } });
    await renderOverview();
    const field = await screen.findByRole('textbox', { name: 'Purpose' });
    await user.clear(field);
    await user.type(field, 'My edit');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText(/Your edits were not saved/i)).toBeInTheDocument();
    expect(field).toHaveValue('My edit');
    expect(within(screen.getByRole('alert')).getByText(/Another client saved revision 2/i)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('not saved');
    expect(updateProduct).toHaveBeenCalledTimes(1);
  });
  it('ui_shared_edit: refreshes clean fields and requires review of remote changes to dirty fields', async () => {
    const user = userEvent.setup();
    let remote = first;
    vi.mocked(getProduct).mockReset().mockImplementation(async () => remote);
    vi.mocked(updateProduct).mockImplementation(async (_productId, _projectId, revision, fields) => {
      if (revision !== remote.revision) throw { status: 412 };
      remote = { ...remote, revision: revision + 1, product: { ...remote.product, ...fields } };
      return remote;
    });
    await renderOverview();
    const field = await screen.findByRole('textbox', { name: 'Purpose' });
    remote = latest;
    await refresh();
    expect(field).toHaveValue('Updated from CLI');
    await user.clear(field);
    await user.type(field, 'My edit');
    remote = { ...remote, revision: 3, product: { ...remote.product, purpose: 'Newest CLI edit' } };
    window.dispatchEvent(new Event('focus'));
    expect(await screen.findByText(/Another client saved revision 3/i)).toBeInTheDocument();
    expect(field).toHaveValue('My edit');
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    expect(updateProduct).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Replace saved fields with my edits' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Saved revision 4');
    expect(remote.product.purpose).toBe('My edit');
    expect(updateProduct).toHaveBeenCalledWith('one', 'two', 3, {
      name: 'Planner', purpose: 'My edit', intended_users: ['Maintainers'],
    }, expect.stringMatching(/^[a-f0-9]{32}$/));
  });

  it('renders the fetched delivery home and caveat once, then reflects a newer profile', async () => {
    const planning = { provider: 'github' as const, repository: 'example/planning', repository_id: 101 };
    const roadmap = { provider: 'github' as const, repository: 'example/roadmap', repository_id: 102 };
    vi.mocked(getProduct).mockReset()
      .mockResolvedValueOnce({ ...first, delivery_home_repository: planning, delivery_home_implied: false })
      .mockResolvedValue({ ...latest, delivery_home_repository: roadmap, delivery_home_implied: false });
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [
      { id: 'binding-one', project_id: 'two', status: 'healthy', selection: { kind: 'repository', provider: 'github', namespace: '27', native_id: '101' } },
      { id: 'binding-two', project_id: 'two', status: 'healthy', selection: { kind: 'repository', provider: 'github', namespace: '27', native_id: '102' } },
    ] });

    await renderOverview();
    await userEvent.click(screen.getByRole('button', { name: 'Manage connections' }));
    await screen.findByText('repository: 102 · healthy');
    const home = screen.getByLabelText('Delivery home');
    expect(screen.getAllByLabelText('Delivery home')).toHaveLength(1);
    expect(within(home).getByText('Delivery home repository:').parentElement).toHaveTextContent('example/planning');
    expect(within(home).getByText('Changing the home moves future epics, not existing ones.')).toBeInTheDocument();

    await refresh();
    await waitFor(() => expect(home).toHaveTextContent('example/roadmap'));
    expect(home).not.toHaveTextContent('example/planning');
    expect(screen.getAllByLabelText('Delivery home')).toHaveLength(1);
  });

});
