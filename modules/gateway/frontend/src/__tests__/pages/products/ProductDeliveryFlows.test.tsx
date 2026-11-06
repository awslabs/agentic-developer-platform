import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import type { ReactNode } from 'react';
import ProductOverview from '@/pages/products/ProductOverview';
import { getProduct, listProductScopes } from '@/services/products';
import { listFlows } from '@/services/orchestration';
import { fetchFeatures, ALL_FEATURES_ENABLED } from '@/services/features';
import { useFlows, type FlowFilters } from '@/hooks/useFlows';
import type { FlowList, FlowSummary } from '@/types/orchestration';

vi.mock('@/pages/products/ProductSources', () => ({ default: () => null }));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'member', orgId: 'tenant' } }) }));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ hasPermission: () => false }) }));
vi.mock('@/pages/products/ProductPeople', () => ({ default: () => null }));
vi.mock('@/pages/products/ProductGitlabHome', () => ({ default: () => null }));
vi.mock('@/services/products', async (original) => ({ ...await original<typeof import('@/services/products')>(), getProduct: vi.fn(), listProductScopes: vi.fn() }));
vi.mock('@/services/orchestration', async (original) => ({ ...await original<typeof import('@/services/orchestration')>(), listFlows: vi.fn() }));
vi.mock('@/services/features', async (original) => ({ ...await original<typeof import('@/services/features')>(), fetchFeatures: vi.fn() }));

const base = '/products/product-a/projects/project-a';
const flow: FlowSummary = {
  id: 'flow-a', slug: 'release', title: 'Release checkout', product_id: 'product-a', project_id: 'project-b',
  intent_ref: null, description: null, design_history: null, status: 'running', awaiting_gate_count: 0, stalled_count: 0,
  display_counts: { queued: 0, in_progress: 1, gate: 0, stalled: 0, complete: 0 },
  total_nodes: 1, epic_count: 1, wave_count: 0, current_wave_ref: null, waves: [],
  delivery_cost: { status: 'known', amount_usd: '0.10', scope: 'agent run costs only; excludes build/infra' },
  created_at: '2026-10-28T00:00:00Z', updated_at: null,
};
const list: FlowList = {
  flows: [flow], total: 1, limit: 25, offset: 0,
  status_counts: { running: 1, complete: 0, awaiting_you: 0, attention_needed: 0, queued: 0, empty: 0, archived: 0 },
};

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}
function FlowDetail() {
  const location = useLocation();
  return <Link to={location.state.flowsReturn}>Back to product flows</Link>;
}
function renderProduct(entry = base) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}>
    <Location /><Routes>
      <Route path="/products/:productId/projects/:projectId/*" element={<ProductOverview />} />
      <Route path="/products/:productId/projects/:projectId/flows" element={<ProductOverview />} />
      <Route path="/flows/:flowId" element={<FlowDetail />} />
    </Routes>
  </MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  vi.mocked(fetchFeatures).mockResolvedValue({ ...ALL_FEATURES_ENABLED, orchestration_engine: true });
  vi.mocked(listProductScopes).mockResolvedValue(['project-a', 'project-b']);
  vi.mocked(getProduct).mockImplementation(async (productId, projectId) => ({
    product_id: productId, project_id: projectId, revision: 1,
    product: { name: 'Checkout', purpose: 'Deliver checkout', intended_users: ['Customers'], owner_ref: { kind: 'user', id: 'owner' } },
  }));
  vi.mocked(listFlows).mockResolvedValue(list);
});

describe('Product Delivery flows tab', () => {
  it('loads the existing list for the selected Project and changes scope when switching Projects', async () => {
    renderProduct();
    const user = userEvent.setup();
    const tab = await screen.findByRole('button', { name: 'Build' });
    expect(listFlows).not.toHaveBeenCalled();
    await user.click(tab);
    expect(await screen.findByText('Release checkout')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(`${base}/build`);
    expect(listFlows).toHaveBeenCalledWith(expect.objectContaining({ product_id: 'product-a' }), expect.any(AbortSignal));
    expect(screen.queryByRole('complementary', { name: 'Product summary' })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Project' }), 'project-b');
    await waitFor(() => expect(getProduct).toHaveBeenCalledWith('product-a', 'project-b'));
    expect(await screen.findByText('Release checkout')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Build' })).toHaveAttribute('aria-current', 'page');
    expect(listFlows).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: 'project-b' }), expect.any(AbortSignal));
    expect(vi.mocked(listFlows).mock.calls.every(([params]) => params?.product_id === 'product-a' && !!params.project_id)).toBe(true);
  });

  it('deep links, filters and returns from details within the Product list', async () => {
    renderProduct(`${base}/flows?status=running&include_archived=true`);
    const user = userEvent.setup();
    const card = await screen.findByTestId('flow-card-flow-a');
    expect(screen.getByRole('button', { name: 'Build' })).toHaveAttribute('aria-current', 'page');
    expect(listFlows).toHaveBeenCalledWith(expect.objectContaining({ product_id: 'product-a', status: 'running', include_archived: true }), expect.any(AbortSignal));
    expect(screen.getByRole('link', { name: 'New flow' })).toHaveAttribute('href', '/flows/new?product=product-a&project=project-a');
    expect(screen.getByRole('link', { name: 'Attention view' })).toHaveAttribute('href', `${base}/flows?needs_me=true&sort=stalled`);
    await user.click(card);
    await user.click(await screen.findByRole('link', { name: 'Back to product flows' }));
    await screen.findByTestId('flow-card-flow-a');
    expect(screen.getByTestId('location')).toHaveTextContent(`${base}/flows?status=running&include_archived=true`);
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(listFlows).toHaveBeenLastCalledWith(expect.objectContaining({ product_id: 'product-a', status: undefined, include_archived: false }), expect.any(AbortSignal)));
  });

  it('keeps Product errors distinct from an empty Product', async () => {
    vi.mocked(listFlows).mockRejectedValue(new Error('Product unavailable or you do not have access.'));
    renderProduct(`${base}/flows`);
    expect(await screen.findByTestId('flows-error')).toHaveTextContent('Product unavailable or you do not have access.');
    expect(screen.queryByTestId('flows-empty')).not.toBeInTheDocument();
  });

  it('does not request flows when the feature is disabled', async () => {
    vi.mocked(fetchFeatures).mockResolvedValue({ ...ALL_FEATURES_ENABLED, orchestration_engine: false });
    renderProduct(`${base}/flows`);
    expect(await screen.findByText('Delivery flows are not enabled in this environment.')).toBeInTheDocument();
    expect(listFlows).not.toHaveBeenCalled();
  });
});

it('does not use another Product list as placeholder data while loading or after denial', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const filters: FlowFilters = { q: '', status: undefined, needsMe: false, includeArchived: false, sort: 'created', offset: 0 };
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result, rerender } = renderHook(({ productId }) => {
    const { data, isError } = useFlows(filters, productId);
    return { data, isError };
  }, { initialProps: { productId: 'product-a' }, wrapper });
  await waitFor(() => expect(result.current.data).toEqual(list));
  let reject!: (error: Error) => void;
  const pending = new Promise<FlowList>((_resolve, fail) => { reject = fail; });
  vi.mocked(listFlows).mockReturnValue(pending);
  rerender({ productId: 'product-b' });
  expect(result.current.data).toBeUndefined();
  await act(async () => reject(new Error('Access denied')));
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(result.current.data).toBeUndefined();
});
