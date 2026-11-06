import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from '@/App';
import { fetchFeatures } from '@/services/features';
import { createProduct, getProduct, listProducts, listProductScopes, type ProductProfile } from '@/services/products';
import * as l5 from '@/services/l5Services';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({
  isAuthenticated: true, isLoading: false, user: { id: 'actor-a', orgId: 'tenant-a', role: 'member' },
  hasPermission: () => false, hasRole: () => false, logout: vi.fn(),
}) }));
vi.mock('@/hooks/useAccessStatus', () => ({ useAccessStatus: () => ({ status: 'registered', isLoading: false }) }));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({
  isPlatformAdmin: () => false, isOrgAdmin: () => false, isDeptAdmin: () => false,
  canViewBudgets: () => false, canViewRateLimits: () => false, hasPermission: () => false,
  canViewLogs: () => false, canViewMetrics: () => false, canViewOrganizations: () => false, canViewPool: () => false,
}) }));
vi.mock('@/components/WorkspaceSelector', () => ({ WorkspaceSelector: () => null }));
vi.mock('@/components/RoleBasedRedirect', () => ({ RoleBasedRedirect: () => <div>Home</div> }));
vi.mock('@/pages/products/ProductPeople', () => ({ default: () => null }));
vi.mock('@/pages/products/ProductGitlabHome', () => ({ default: () => null }));
vi.mock('@/services/features', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/features')>(), fetchFeatures: vi.fn(),
}));
vi.mock('@/services/products', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/products')>(),
  createProduct: vi.fn(), getProduct: vi.fn(), listProducts: vi.fn(), listProductScopes: vi.fn(),
}));
vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(),
  getSecurityOptions: vi.fn(), getSecurityConfiguration: vi.fn(),
  getDevOpsOptions: vi.fn(), getDevOpsConfiguration: vi.fn(),
  getFinOpsOptions: vi.fn(), getFinOpsConfiguration: vi.fn(),
  getTransformOptions: vi.fn(), getTransformConfiguration: vi.fn(),
  getWellArchitectedOptions: vi.fn(), getWellArchitectedConfiguration: vi.fn(),
  submitSecurityRun: vi.fn(), submitDevOpsRun: vi.fn(), submitFinOpsRun: vi.fn(), submitTransformRun: vi.fn(), submitWellArchitectedRun: vi.fn(),
}));

const profile: ProductProfile = { product_id: 'product-1', project_id: 'project-1', tenant_id: 'tenant-a', configuration: {}, revision: 1, product: {
  name: 'Example Planner', purpose: 'Coordinate small releases', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' },
} };

let go: ReturnType<typeof useNavigate>;
function RouteObservation() {
  go = useNavigate();
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

async function renderApp(path = '/products') {
  // Settle the real feature query and lazy route before starting user actions.
  // Otherwise the one-second element wait also times cold route initialization.
  await act(async () => {
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}><App /><RouteObservation /></MemoryRouter>
    </QueryClientProvider>);
  });
}

describe('production Product routes', () => {
  beforeAll(async () => {
    // Load the real route chunks before the interaction assertions. Vite's cold
    // transforms can exceed findByRole's timeout under concurrent CI workers.
    // App still exercises its production lazy routes and feature gates.
    await Promise.all([
      import('@/pages/products/ProductsList'),
      import('@/pages/products/ProductCreate'),
      import('@/pages/products/ProductOverview'),
    ]);
  });

  beforeEach(() => {
    vi.resetAllMocks();
    sessionStorage.clear();
    vi.mocked(fetchFeatures).mockResolvedValue({ product_profile: true } as Awaited<ReturnType<typeof fetchFeatures>>);
    vi.mocked(listProductScopes).mockResolvedValue(['project-1']);
    const options = { connections: [], sources: [], regions: [], capabilities: [], unavailable_reasons: [] };
    for (const read of [l5.getSecurityOptions, l5.getDevOpsOptions, l5.getFinOpsOptions, l5.getWellArchitectedOptions]) {
      vi.mocked(read).mockResolvedValue({ ...options, admissions_enabled: false });
    }
    vi.mocked(l5.getTransformOptions).mockResolvedValue({ ...options, service_id: 'transform' });
    for (const read of [l5.getSecurityConfiguration, l5.getDevOpsConfiguration, l5.getFinOpsConfiguration, l5.getTransformConfiguration, l5.getWellArchitectedConfiguration]) {
      vi.mocked(read).mockRejectedValue({ status: 404 });
    }
  });

  it('ui_minimal_save: reaches the real create screen and reads a persisted Product independently', async () => {
    const user = userEvent.setup();
    let stored: ProductProfile | null = null;
    vi.mocked(listProducts).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(createProduct).mockImplementation(async () => { stored = profile; return profile; });
    vi.mocked(getProduct).mockImplementation(async () => {
      if (!stored) throw new Error('Missing independent readback');
      return stored;
    });
    await renderApp();
    await user.click(await screen.findByRole('link', { name: 'Create product' }));
    await user.type(await screen.findByRole('textbox', { name: 'Name' }), 'Example Planner');
    await user.type(screen.getByRole('textbox', { name: 'Purpose' }), 'Coordinate small releases');
    await user.type(screen.getByRole('textbox', { name: 'Intended users' }), 'Maintainers');
    await user.click(screen.getByRole('button', { name: 'Save product' }));
    expect(await screen.findByRole('heading', { name: 'Example Planner' })).toBeInTheDocument();
    expect(getProduct).toHaveBeenCalledWith('product-1', 'project-1');
    expect(stored).toEqual(profile);
  });

  it('does not expose Product navigation or content when the backend flag is off', async () => {
    vi.mocked(fetchFeatures).mockResolvedValue({ product_profile: false } as Awaited<ReturnType<typeof fetchFeatures>>);
    await renderApp();
    expect(await screen.findByText('Home')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Products' })).not.toBeInTheDocument();
    expect(createProduct).not.toHaveBeenCalled();
  });

  const services = [
    ['security', 'Security', l5.getSecurityConfiguration], ['devops', 'DevOps', l5.getDevOpsConfiguration],
    ['finops', 'FinOps', l5.getFinOpsConfiguration], ['transform', 'Transform', l5.getTransformConfiguration],
    ['well_architected', 'Well-Architected', l5.getWellArchitectedConfiguration],
  ] as const;
  const deferred = {
    context: { status: 'unavailable', reason: 'integration_deferred', message: 'Context integration is deferred.' },
    l5: { status: 'unavailable', reason: 'integration_deferred', message: 'L5 integration is deferred.' },
  } as const;
  const assertNoRuns = () => {
    for (const submit of [l5.submitSecurityRun, l5.submitDevOpsRun, l5.submitFinOpsRun, l5.submitTransformRun, l5.submitWellArchitectedRun]) expect(submit).not.toHaveBeenCalled();
  };

  it.each([undefined, deferred])('offers all services on an incomplete saved Product, including deferred profile sharing (%j)', async peer_availability => {
    vi.mocked(getProduct).mockResolvedValue({ ...profile, peer_availability });
    await renderApp('/products/product-1/projects/project-1');
    await userEvent.click(await screen.findByRole('button', { name: 'Agents', exact: true }));
    const choices = await screen.findByRole('region', { name: 'Product agents' });
    expect(within(choices).getAllByRole('link')).toHaveLength(5);
    for (const [id, name, read] of services) {
      expect(within(choices).getByRole('link', { name: `Open ${name}` })).toHaveAttribute('href', `/products/product-1/projects/project-1/agents/${id}`);
      expect(read).not.toHaveBeenCalled();
    }
    if (peer_availability) expect(screen.getByText('L5 integration is deferred.')).not.toBeVisible();
    else expect(screen.queryByText('L5 integration is deferred.')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Agents', exact: true }));
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/products/product-1/projects/project-1/agents');
    expect(screen.getByRole('region', { name: 'Product agents' })).toBeVisible();
    for (const [, , read] of services) expect(read).not.toHaveBeenCalled();
    assertNoRuns();
  });

  it.each(services)('opens the real %s journey from Overview without a ready configuration', async (id, name, read) => {
    vi.mocked(getProduct).mockResolvedValue({ ...profile, peer_availability: deferred });
    await renderApp('/products/product-1/projects/project-1');
    await userEvent.click(await screen.findByRole('button', { name: 'Agents', exact: true }));
    const link = await screen.findByRole('link', { name: `Open ${name}` });
    link.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByRole('navigation', { name: `${name} service sections` })).toBeVisible();
    await waitFor(() => expect(read).toHaveBeenCalledWith('product-1', 'project-1'));
    expect(screen.getByLabelText('Current route')).toHaveTextContent(`/products/product-1/projects/project-1/agents/${id}`);
    const sections = within(screen.getByRole('navigation', { name: `${name} service sections` }));
    for (const section of ['Setup', 'Runs & schedules', 'Results']) {
      await userEvent.click(sections.getByRole('link', { name: section, exact: true }));
      expect(screen.getByRole('region', { name: section, exact: true })).toBeVisible();
      expect(document.querySelectorAll('.l5-service-section:not([hidden])')).toHaveLength(section === 'Setup' && !['devops', 'security'].includes(id) ? 2 : 1);
      expect(document.querySelectorAll('.l5-service-section')).toHaveLength(['devops', 'security'].includes(id) ? 3 : 4);
    }
    await userEvent.click(sections.getByRole('link', { name: 'Setup' }));
    expect(screen.queryByRole('navigation', { name: 'Product services' })).not.toBeInTheDocument();
    if (id === 'security') expect(screen.getByText('Pen testing · Not available yet')).toBeVisible();
    if (id === 'finops') expect(within(screen.getByRole('article', { name: 'Cost inquiry' })).getByText('Not available yet')).toBeVisible();
    assertNoRuns();
  });

  it('honors deep links and browser history without leaving a service selected on Overview', async () => {
    vi.mocked(getProduct).mockResolvedValue(profile);
    await renderApp('/products/product-1/projects/project-1/services');
    await userEvent.click(await screen.findByRole('link', { name: 'Open DevOps' }));
    await screen.findByRole('navigation', { name: 'DevOps service sections' });
    await userEvent.click(screen.getByRole('button', { name: 'Agents', exact: true }));
    await userEvent.click(screen.getByRole('link', { name: 'Open Security' }));
    await screen.findByRole('navigation', { name: 'Security service sections' });
    await act(async () => go(-2));
    expect(await screen.findByRole('navigation', { name: 'DevOps service sections' })).toBeVisible();
    await act(async () => go(-1));
    expect(await screen.findByRole('region', { name: 'Product agents' })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Profile', exact: true }));
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/products/product-1/projects/project-1');
    expect(screen.queryByRole('navigation', { name: 'DevOps service sections' })).not.toBeInTheDocument();
    assertNoRuns();
  });

  it('keeps the selected service when changing delivery scope and reads only the new scope', async () => {
    vi.mocked(listProductScopes).mockResolvedValue(['project-1', 'project-2']);
    vi.mocked(getProduct).mockImplementation(async (_productId, projectId) => ({ ...profile, project_id: projectId }));
    await renderApp('/products/product-1/projects/project-1/services/devops');
    await waitFor(() => expect(l5.getDevOpsConfiguration).toHaveBeenCalledWith('product-1', 'project-1'));
    await userEvent.selectOptions(await screen.findByLabelText('Project'), 'project-2');
    await waitFor(() => expect(l5.getDevOpsConfiguration).toHaveBeenLastCalledWith('product-1', 'project-2'));
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/products/product-1/projects/project-2/services/devops');
    expect(screen.getByRole('navigation', { name: 'DevOps service sections' })).toBeVisible();
    expect(l5.getSecurityConfiguration).not.toHaveBeenCalled();
    assertNoRuns();
  });

  it('withholds the chooser when the Product read is denied', async () => {
    vi.mocked(getProduct).mockRejectedValue({ status: 403 });
    await renderApp('/products/product-1/projects/project-1/services');
    expect(await screen.findByRole('alert')).toHaveTextContent('Product unavailable or you do not have access');
    expect(screen.queryByRole('navigation', { name: 'Choose a Product service' })).not.toBeInTheDocument();
    for (const [, , read] of services) expect(read).not.toHaveBeenCalled();
  });
});
