import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ProductsList from '@/pages/products/ProductsList';
import ProductCreate from '@/pages/products/ProductCreate';
import ProductOverview from '@/pages/products/ProductOverview';
import { getProjectBudget, getProductPeople, createProduct, createScope, getProduct, linkScope, listPortfolios, listPortfolioMemberships, listProducts, listProductScopes, listProjectBindings, listProjects, unlinkScope, updateProduct, updateProject } from '@/services/products';

vi.mock('@/pages/products/ProductSources', () => ({ default: () => null }));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'actor-a', orgId: 'tenant-a' } }) }));

vi.mock('@/services/products', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/products')>(),
  getProjectBudget: vi.fn(), getProductPeople: vi.fn(),
  createProduct: vi.fn(), createScope: vi.fn(), getProduct: vi.fn(), linkScope: vi.fn(), listPortfolios: vi.fn(), listPortfolioMemberships: vi.fn(), listProducts: vi.fn(), listProductScopes: vi.fn(), listProjectBindings: vi.fn(), listProjects: vi.fn(), unlinkScope: vi.fn(), updateProduct: vi.fn(), updateProject: vi.fn(),
}));

const profile = {
  product_id: 'product-1', project_id: 'project-1', revision: 1,
  product: { name: 'Planner', purpose: 'Coordinate releases', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'owner' } },
};

async function renderRoutes(path: string) {
  const view = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/products" element={<ProductsList />} />
    <Route path="/products/new" element={<ProductCreate />} />
    <Route path="/products/:productId/projects/:projectId/*" element={<ProductOverview />} />
  </Routes></MemoryRouter></QueryClientProvider>);
  if (path.includes('/projects/')) {
    await screen.findByRole('button', { name: 'New project', exact: true });
    await userEvent.click(screen.getByText('Current project settings', { selector: 'summary' }));
  }
  return view;
}

describe('Product screens', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getProjectBudget).mockImplementation(async projectId => ({ product_id: 'product-1', project_id: projectId, project_name: 'General', is_base: true, budget_usd: null, spent_usd: 0, reserved_usd: 0, available_usd: null, accounting_status: 'current', can_manage: false, flows: [] }));
    vi.mocked(getProductPeople).mockResolvedValue({ product_id: 'product-1', revision: 1, owner: { id: 'actor-a', name: 'Owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    sessionStorage.clear();
    vi.mocked(listProducts).mockResolvedValue({ items: [{ id: 'product-1', name: 'Planner', revision: 1, project_ids: ['project-1'] }], next_cursor: null });
    vi.mocked(createProduct).mockResolvedValue(profile);
    vi.mocked(getProduct).mockResolvedValue(profile);
    vi.mocked(listProductScopes).mockResolvedValue(['project-1']);
    vi.mocked(listProjects).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(listProjectBindings).mockResolvedValue({ items: [] });
  });

  it('scope_admin_states: hands a non-owner editor to administration without restricted choices or blocked edits', async () => {
    const user = userEvent.setup();
    vi.mocked(getProductPeople).mockResolvedValue({ product_id: 'product-1', revision: 1, owner: { id: 'owner', name: 'Owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: false, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    let latest = { ...profile, project: { id: 'project-1', name: 'Alpha' } };
    vi.mocked(getProduct).mockImplementation(async () => latest);
    vi.mocked(listProjects).mockResolvedValue({ items: [{ id: 'secret-project', name: 'Restricted Project', product_id: null }], next_cursor: null });
    vi.mocked(updateProduct).mockImplementation(async (_product, _project, revision, fields) => {
      latest = { ...latest, revision: revision + 1, product: { ...latest.product, ...fields } };
      return latest;
    });
    await renderRoutes('/products/product-1/projects/project-1');

    expect(screen.getByRole('button', { name: 'New project', exact: true })).toBeDisabled();
    expect(screen.getByText('The product owner can create or link projects.')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Existing Project' })).not.toBeInTheDocument();
    expect(screen.queryByText('Restricted Project')).not.toBeInTheDocument();
    expect(listProjects).not.toHaveBeenCalled();
    expect(listProjectBindings).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Profile' }));
    await user.click(screen.getByRole('button', { name: 'Edit information' }));
    const purpose = screen.getByRole('textbox', { name: 'Purpose' });
    await user.clear(purpose);
    await user.type(purpose, 'Editor can still work');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(updateProduct).toHaveBeenCalledWith('product-1', 'project-1', 1, expect.objectContaining({ purpose: 'Editor can still work' }), expect.any(String)));
    expect(await screen.findByText('Saved revision 2', { selector: 'p[role="status"]' })).toBeInTheDocument();
  });

  it('omits already-bound Projects from eligible choices without showing a hidden count', async () => {
    const user = userEvent.setup();
    vi.mocked(getProduct).mockResolvedValue({ ...profile, product: { ...profile.product, owner_ref: { kind: 'user', id: 'actor-a' } } });
    vi.mocked(listProjects).mockResolvedValue({ items: [
      { id: 'eligible', name: 'Eligible Project', product_id: null },
      { id: 'foreign-bound', name: 'Already bound', product_id: 'other-product' },
    ], next_cursor: null });
    await renderRoutes('/products/product-1/projects/project-1');
    await user.click(await screen.findByText('Link an existing project'));
    expect(await screen.findByRole('option', { name: 'Eligible Project' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Already bound' })).not.toBeInTheDocument();
    expect(screen.queryByText(/hidden|more available|total projects/i)).not.toBeInTheDocument();
  });

  it.each(['legacy', 'registry'])('adds a second Project from Projects using %s binding history and switches to its scoped profile', async (format) => {
    const user = userEvent.setup();
    const owner = { ...profile, project: { id: 'project-1', name: 'Alpha' }, product: { ...profile.product, owner_ref: { kind: 'user', id: 'actor-a' } } };
    const second = { ...owner, project_id: 'project-2', project: { id: 'project-2', name: 'Beta' }, revision: 2 };
    vi.mocked(getProduct).mockImplementation(async (_product, project) => project === 'project-2' ? second : owner);
    vi.mocked(createScope).mockResolvedValue(second);
    vi.mocked(listProductScopes).mockResolvedValueOnce(['project-1']).mockResolvedValue(['project-1', 'project-2']);
    await renderRoutes('/products/product-1/projects/project-1');
    vi.mocked(listProjectBindings).mockResolvedValue({ items: [{ relationship_versions: [format === 'legacy'
      ? { binding_id: 'binding-alpha', project_id: 'project-1' }
      : { relationship_id: 'binding-alpha', to_ref: { record_type: 'project', record_id: 'project-1', project_id: 'project-1' } },
    ] }] });
    await user.click(await screen.findByText('Link an existing project'));
    expect(await screen.findByText('Binding: binding-alpha')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'New project', exact: true }));
    await user.type(screen.getByRole('textbox', { name: 'New project name' }), 'Beta');
    await user.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(createScope).toHaveBeenCalledWith('product-1', 1, 'Beta', expect.any(String)));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Project name' })).toHaveValue('Beta'));
    expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('project-2');
    expect(screen.getByRole('heading', { name: 'Planner' })).toBeInTheDocument();
  });

  it('retries an uncertain Project creation with the original receipt after remount', async () => {
    const user = userEvent.setup();
    const owner = { ...profile, project: { id: 'project-1', name: 'Alpha' }, product: { ...profile.product, owner_ref: { kind: 'user', id: 'actor-a' } } };
    const second = { ...owner, revision: 2, project_id: 'project-2', project: { id: 'project-2', name: 'Beta' } };
    vi.mocked(getProduct).mockImplementation(async (_product, project) => project === 'project-2' ? second : owner);
    let created = false;
    vi.mocked(listProductScopes).mockImplementation(async () => created ? ['project-1', 'project-2'] : ['project-1']);
    vi.mocked(createScope).mockRejectedValueOnce(new TypeError('Response dropped')).mockImplementation(async () => { created = true; return second; });
    const view = await renderRoutes('/products/product-1/projects/project-1');
    await user.click(await screen.findByText('Link an existing project'));
    await user.click(screen.getByRole('button', { name: 'New project', exact: true }));
    await user.type(screen.getByRole('textbox', { name: 'New project name' }), 'Beta');
    await user.click(screen.getByRole('button', { name: 'Create project' }));
    expect(await screen.findByText(/Couldn't confirm the binding change/i)).toBeInTheDocument();
    const original = vi.mocked(createScope).mock.calls[0];
    view.unmount();
    await renderRoutes('/products/product-1/projects/project-1');
    await user.click(await screen.findByText('Link an existing project'));
    await user.click(await screen.findByRole('button', { name: 'Check project save status' }));
    await waitFor(() => expect(createScope).toHaveBeenCalledTimes(2));
    expect(vi.mocked(createScope).mock.calls[1]).toEqual(original);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('project-2'));
  });

  it('links and unlinks an authorized existing Project without hiding saved configuration', async () => {
    const user = userEvent.setup();
    const owner = { ...profile, project: { id: 'project-1', name: 'Alpha' }, product: { ...profile.product, owner_ref: { kind: 'user', id: 'actor-a' } } };
    const linked = { ...owner, project_id: 'project-2', project: { id: 'project-2', name: 'Existing' }, revision: 2 };
    vi.mocked(getProduct).mockImplementation(async (_product, project) => project === 'project-2' ? linked : owner);
    vi.mocked(listProjects).mockResolvedValue({ items: [{ id: 'project-2', name: 'Existing', product_id: null }], next_cursor: null });
    vi.mocked(linkScope).mockResolvedValue(linked);
    vi.mocked(unlinkScope).mockResolvedValue({ ...owner, revision: 3 });
    vi.mocked(listProductScopes).mockResolvedValueOnce(['project-1']).mockResolvedValue(['project-1', 'project-2']);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await renderRoutes('/products/product-1/projects/project-1');
    await user.click(await screen.findByText('Link an existing project'));
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Existing Project' }), 'project-2');
    await user.click(screen.getByRole('button', { name: 'Link Project' }));
    await waitFor(() => expect(linkScope).toHaveBeenCalledWith('product-1', 1, 'project-2', expect.any(String)));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('project-2'));
    const settings = await screen.findByText('Current project settings', { selector: 'summary' });
    if (!(settings.closest('details') as HTMLDetailsElement).open) await user.click(settings);
    expect(await screen.findByRole('textbox', { name: 'Project name' })).toHaveValue('Existing');
    await user.click(await screen.findByRole('button', { name: 'Unlink this Project' }));
    await waitFor(() => expect(unlinkScope).toHaveBeenCalledWith('product-1', 2, 'project-2', expect.any(String)));
  });

  it('scope_drafts_and_links: keeps a dirty scoped draft through link and unlink', async () => {
    const user = userEvent.setup();
    let linked = false;
    let unlinked = false;
    const owner = { ...profile, project: { id: 'project-1', name: 'Alpha' }, product: { ...profile.product, owner_ref: { kind: 'user', id: 'actor-a' } } };
    const existing = { ...owner, project_id: 'project-2', project: { id: 'project-2', name: 'Beta' }, revision: 2 };
    vi.mocked(getProduct).mockImplementation(async (_product, project) => project === 'project-2' ? existing
      : { ...owner, revision: unlinked ? 3 : linked ? 2 : 1 });
    vi.mocked(listProductScopes).mockImplementation(async () => linked && !unlinked ? ['project-1', 'project-2'] : ['project-1']);
    vi.mocked(listProjects).mockResolvedValue({ items: [{ id: 'project-2', name: 'Beta', product_id: null }], next_cursor: null });
    vi.mocked(linkScope).mockImplementation(async () => { linked = true; return existing; });
    vi.mocked(unlinkScope).mockImplementation(async () => { unlinked = true; return { ...owner, revision: 3 }; });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await renderRoutes('/products/product-1/projects/project-1');
    const name = await screen.findByRole('textbox', { name: 'Project name' });
    await user.clear(name);
    await user.type(name, 'Alpha draft');
    await user.click(screen.getByText('Link an existing project'));
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Existing Project' }), 'project-2');
    await user.click(screen.getByRole('button', { name: 'Link Project' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Project name' })).toHaveValue('Beta'));
    expect(getProduct).toHaveBeenCalledWith('product-1', 'project-2');
    const more = await screen.findByText('Link an existing project');
    if (!more.closest('details')?.open) await user.click(more);
    await user.click(await screen.findByRole('button', { name: 'Unlink this Project' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Project name' })).toHaveValue('Alpha draft'));
    expect(screen.getByRole('heading', { name: 'Planner' })).toBeInTheDocument();
    expect(linkScope).toHaveBeenCalledWith('product-1', 1, 'project-2', expect.any(String));
    expect(unlinkScope).toHaveBeenCalledWith('product-1', 2, 'project-2', expect.any(String));
    expect(sessionStorage.getItem('product-draft:actor-a:tenant-a:project:product-1:project-1')).toContain('Alpha draft');
  });

  it('retains separate unsaved Project names when switching away and back', async () => {
    const user = userEvent.setup();
    vi.mocked(listProductScopes).mockResolvedValue(['project-1', 'project-2']);
    vi.mocked(getProduct).mockImplementation(async (_product, project) => ({
      ...profile, project_id: project, project: { id: project, name: project === 'project-1' ? 'Alpha' : 'Beta' },
    }));
    await renderRoutes('/products/product-1/projects/project-1');
    const selector = await screen.findByRole('combobox', { name: 'Project' });
    const scopeName = await screen.findByRole('textbox', { name: 'Project name' });
    await user.clear(scopeName);
    await user.type(scopeName, 'Alpha draft');
    await user.selectOptions(selector, 'project-2');
    expect(await screen.findByRole('textbox', { name: 'Project name' })).toHaveValue('Beta');
    await user.clear(screen.getByRole('textbox', { name: 'Project name' }));
    await user.type(screen.getByRole('textbox', { name: 'Project name' }), 'Beta draft');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Project' }), 'project-1');
    expect(await screen.findByRole('textbox', { name: 'Project name' })).toHaveValue('Alpha draft');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Project' }), 'project-2');
    expect(await screen.findByRole('textbox', { name: 'Project name' })).toHaveValue('Beta draft');
    expect(screen.getByRole('heading', { name: 'Planner' })).toBeInTheDocument();
  });

  it('keeps a dirty Project draft when its saved revision changes', async () => {
    const user = userEvent.setup();
    const first = { ...profile, project: { id: 'project-1', name: 'Alpha' } };
    let remote = first;
    vi.mocked(getProduct).mockImplementation(async () => remote);
    await renderRoutes('/products/product-1/projects/project-1');
    const field = await screen.findByRole('textbox', { name: 'Project name' });
    await user.clear(field);
    await user.type(field, 'Alpha draft');
    remote = { ...first, revision: 2, project: { id: 'project-1', name: 'Remote Alpha' } };
    window.dispatchEvent(new Event('focus'));
    expect(await screen.findByText(/Project changed elsewhere. Your draft is retained/i)).toBeInTheDocument();
    expect(field).toHaveValue('Alpha draft');
    expect(screen.getByRole('button', { name: 'Save Project' })).toBeDisabled();
    expect(updateProject).not.toHaveBeenCalled();
  });

  it('saves only the selected Project name and reads back the same Project ID', async () => {
    const user = userEvent.setup();
    const first = { ...profile, project: { id: 'project-1', name: 'Alpha' } };
    const saved = { ...first, revision: 2, project: { id: 'project-1', name: 'New Alpha' } };
    vi.mocked(getProduct).mockResolvedValueOnce(first).mockResolvedValue(saved);
    vi.mocked(updateProject).mockResolvedValue(saved);
    await renderRoutes('/products/product-1/projects/project-1');
    const name = await screen.findByRole('textbox', { name: 'Project name' });
    await user.clear(name);
    await user.type(name, 'New Alpha');
    await user.click(screen.getByRole('button', { name: 'Save Project' }));
    await waitFor(() => expect(updateProject).toHaveBeenCalledWith('product-1', 'project-1', 1, 'New Alpha', expect.any(String)));
    expect(await screen.findByText('Project saved.')).toBeInTheDocument();
    expect(getProduct).toHaveBeenCalledWith('product-1', 'project-1');
    expect(screen.getByRole('heading', { name: 'Planner' })).toBeInTheDocument();
    expect(sessionStorage.getItem('product-draft:actor-a:tenant-a:project:product-1:project-1')).toBeNull();
  });

  it('switches to another authorized Project without changing shared Product identity', async () => {
    const user = userEvent.setup();
    vi.mocked(listProductScopes).mockResolvedValue(['project-1', 'project-2']);
    vi.mocked(getProduct).mockImplementation(async (_product, project) => ({ ...profile, project_id: project }));
    await renderRoutes('/products/product-1/projects/project-1');
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Project' }), 'project-2');
    await waitFor(() => expect(getProduct).toHaveBeenCalledWith('product-1', 'project-2'));
    expect(await screen.findByRole('heading', { name: 'Planner' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Project' })).toHaveValue('project-2');
  });

  it('lists authorized results and loads a scoped overview without exposing a one-scope selector', async () => {
    const user = userEvent.setup();
    await renderRoutes('/products');
    await user.click(await screen.findByRole('link', { name: 'Planner' }));
    expect(await screen.findByRole('region', { name: 'Product details and team' })).toHaveTextContent('Coordinate releases');
    expect(getProduct).toHaveBeenCalledWith('product-1', 'project-1');
    expect(screen.queryByRole('combobox', { name: /delivery scope/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/not yet enabled for this product/i)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'People and access' })).toBeInTheDocument();
  });

  it('saves only the three inputs and independently reads back before showing a saved overview', async () => {
    const user = userEvent.setup();
    await renderRoutes('/products/new');
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Planner');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'Coordinate releases');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    await waitFor(() => expect(getProduct).toHaveBeenCalledWith('product-1', 'project-1'));
    expect(createProduct).toHaveBeenCalledWith('Planner', 'Coordinate releases', ['Maintainers'], expect.stringMatching(/^[a-f0-9]{32}$/));
    expect(await screen.findByText('Saved revision 1')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /budget|repository/i })).not.toBeInTheDocument();
  });
  it('saves three-field Product details and later edits despite optional catalog outage', async () => {
    const user = userEvent.setup();
    let latest = { ...profile };
    vi.mocked(getProduct).mockImplementation(async () => latest);
    vi.mocked(listPortfolios).mockRejectedValue({ status: 503, code: 'dependency_unavailable' });
    vi.mocked(updateProduct).mockImplementation(async (_productId, _projectId, revision, fields) => {
      latest = { ...latest, revision: revision + 1, product: { ...latest.product, ...fields } };
      return latest;
    });
    await renderRoutes('/products/new');
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Planner');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'Coordinate releases');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(await screen.findByText('Saved revision 1')).toBeInTheDocument();
    expect(listPortfolios).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: 'Profile' }));
    await user.click(screen.getByText('Organization and Portfolios'));
    expect(await screen.findByText(/Portfolio coverage unavailable/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Profile' }));
    await user.click(screen.getByRole('button', { name: 'Edit information' }));
    const purpose = screen.getByRole('textbox', { name: 'Purpose' });
    await user.clear(purpose);
    await user.type(purpose, 'Edited during catalog outage');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(updateProduct).toHaveBeenCalledWith('product-1', 'project-1', 1,
      expect.objectContaining({ purpose: 'Edited during catalog outage' }), expect.any(String)));
    expect(await screen.findByText('Saved revision 2', { selector: 'p[role="status"]' })).toBeInTheDocument();
    expect(listPortfolioMemberships).not.toHaveBeenCalled();
  });
});
