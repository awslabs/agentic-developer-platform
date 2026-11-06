import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ProductOverview from '@/pages/products/ProductOverview';
import { getSharedProductProfile, getProduct, getProductPeople, getProjectBudget, listProductScopes, listProductSources, listSourceBindings } from '@/services/products';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'owner', orgId: 'tenant' }, hasRole: () => false }) }));
vi.mock('@/pages/products/ProductGitlabHome', () => ({ default: () => null }));
vi.mock('@/pages/products/JiraContextSources', () => ({ default: () => null }));
vi.mock('@/pages/products/ProductOnboarding', () => ({ default: () => <div>Financial planning form</div> }));
vi.mock('@/pages/products/ProductBuild', () => ({ default: () => <div>Build workspace</div> }));
vi.mock('@/services/products', async original => ({
  ...await original<typeof import('@/services/products')>(), getSharedProductProfile: vi.fn(), getProduct: vi.fn(), getProductPeople: vi.fn(),
  getProjectBudget: vi.fn(), listProductScopes: vi.fn(), listProductSources: vi.fn(), listSourceBindings: vi.fn(),
}));

const base = '/products/product-one/projects/project-one';
let go: ReturnType<typeof useNavigate>;
function Location() {
  const location = useLocation(); go = useNavigate();
  return <output aria-label="Current route">{location.pathname}{location.search}{location.hash}</output>;
}
function mount(path = base) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><Routes><Route path="/products/:productId/projects/:projectId/*" element={<ProductOverview />} /></Routes><Location /></MemoryRouter>
  </QueryClientProvider>);
}

describe('Product profile navigation', () => {
  beforeEach(() => {
    vi.resetAllMocks(); sessionStorage.clear();
    vi.mocked(getProduct).mockImplementation(async (_product, projectId) => ({
      product_id: 'product-one', project_id: projectId, revision: 1,
      product: { name: 'Planner', purpose: 'Coordinate releases', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'owner' } },
      project: { id: projectId, name: projectId === 'project-one' ? 'General' : 'Mobile release' },
    }));
    vi.mocked(getProductPeople).mockResolvedValue({ product_id: 'product-one', revision: 1, owner: { id: 'owner', name: 'Example owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    vi.mocked(listProductScopes).mockImplementation(async (_id, names, baseProject) => {
      names?.({ 'project-one': 'General', 'project-two': 'Mobile release' }); baseProject?.('project-one');
      return ['project-one', 'project-two'];
    });
    vi.mocked(getSharedProductProfile).mockResolvedValue({ product_id: 'product-one', revision: 1, base_project_id: 'project-one', can_edit: true, resources: [], environments: [], not_applicable: [], knowledge_count: 0, completion: { completed: 1, applicable: 5, percentage: 20, sections: [] } });
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [] });
    vi.mocked(getProjectBudget).mockResolvedValue({ product_id: 'product-one', project_id: 'project-one', project_name: 'General', is_base: true, budget_usd: 100, spent_usd: 0, reserved_usd: 0, available_usd: 100, accounting_status: 'current', can_manage: false, flows: [] });
  });

  it('opens one profile with details, connections and projects and edits in place', async () => {
    mount();
    const nav = await screen.findByRole('navigation', { name: 'Product workspace' });
    expect(within(nav).getAllByRole('button').map(button => button.textContent)).toEqual(['Profile', 'Build', 'Release', 'Operate', 'Insights', 'Agents', 'Knowledge', 'Budget & spend']);
    expect(within(nav).getByRole('button', { name: 'Profile' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('region', { name: 'Product details and team' })).toHaveTextContent('Coordinate releases');
    expect(screen.getByRole('region', { name: 'Code repositories' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Project sources' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Product projects' })).toHaveTextContent('Mobile release');
    expect(screen.getByText('Base', { exact: true })).toBeVisible();
    await waitFor(() => expect(screen.getByRole('button', { name: 'New project', exact: true })).toBeEnabled());
    expect(screen.queryByRole('button', { name: 'Product settings' })).not.toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Profile setup sections' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Edit information' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Purpose' }), ' together');
    await userEvent.click(screen.getByRole('button', { name: 'Close details editor' }));
    expect(screen.getByText(/You have unsaved product details/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Build', exact: true }));
    await act(async () => go(-1));
    await userEvent.click(screen.getByRole('button', { name: 'Edit information' }));
    expect(screen.getByRole('textbox', { name: 'Purpose' })).toHaveValue('Coordinate releases together');
  });

  it.each([
    ['/settings', '#product-details', true],
    ['/settings?section=basic', '#product-connections', false],
    ['/settings?section=advanced', '#product-connections', false],
    ['/projects', '#product-projects', false],
  ])('preserves the old %s link', async (suffix, hash, editing) => {
    mount(base + suffix);
    await screen.findByRole('heading', { name: 'Planner' });
    expect(screen.getByLabelText('Current route')).toHaveTextContent(base + hash);
    expect(screen.getByRole('button', { name: 'Profile' })).toHaveAttribute('aria-current', 'page');
    expect(!!screen.queryByRole('textbox', { name: 'Purpose' })).toBe(editing);
  });

  it('moves a saved financials link to Budget & spend and opens its plan', async () => {
    mount(base + '/settings?section=financials');
    expect(await screen.findByText('Financial planning form')).toBeVisible();
    expect(screen.getByLabelText('Current route')).toHaveTextContent(base + '/budget#financial-planning');
    expect(screen.getByRole('button', { name: 'Budget & spend' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('region', { name: 'Product details and team' })).not.toBeInTheDocument();
  });

  it('keeps product connections on the Base project when switching the workspace project', async () => {
    mount(base);
    await screen.findByRole('heading', { name: 'Planner' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Project', exact: true }), 'project-two');
    await waitFor(() => expect(screen.getByLabelText('Current route')).toHaveTextContent('/products/product-one/projects/project-two'));
    await userEvent.click(screen.getByRole('button', { name: 'Manage connections' }));
    await screen.findByRole('region', { name: 'Project sources' });
    expect(listProductSources).toHaveBeenLastCalledWith('product-one', 'project-one');
    expect(screen.getByRole('combobox', { name: 'Project using these connections' })).toHaveValue('project-one');
    expect(getSharedProductProfile).toHaveBeenLastCalledWith('product-one');
  });
});
