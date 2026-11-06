import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ProductBuild from '@/pages/products/ProductBuild';
import { getMyInvocations, getMyInvocationDetail, getMyChains } from '@/services/activity';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'owner', orgId: 'tenant' }, hasPermission: () => false }) }));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ isPlatformAdmin: () => true, isOrgAdmin: () => false }) }));
vi.mock('@/hooks/useFeatures', () => ({ useRevalidatingFeaturesQuery: () => ({ data: {}, isPending: false, isError: false }) }));
vi.mock('@/services/activity', () => ({ getMyInvocations: vi.fn(), getMyInvocationDetail: vi.fn(), getMyChains: vi.fn() }));
vi.mock('@/pages/products/ProductDeliveryFlows', () => ({ default: ({ projectId }: { projectId: string }) => <p>Delivery flows for {projectId}</p> }));
const item = { invocation_id: 'run-one', topic: 'Implement checkout', persona: 'developer', status: 'in_progress', invoked_at: '2026-11-03T10:00:00Z' };
const scope = { productId: 'product', projectId: 'project', readerKey: 'tenant:owner' };
function mount(path = '/products/product/projects/project/build/activity') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (projectId: string) => <QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><ProductBuild productId="product" projectId={projectId} /></MemoryRouter></QueryClientProvider>;
  return { client, wrap, ...render(wrap('project')) };
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getMyInvocations).mockResolvedValue({ items: [item], last_key: null } as never);
  vi.mocked(getMyInvocationDetail).mockResolvedValue(item as never);
  vi.mocked(getMyChains).mockResolvedValue({ chains: [], last_key: null, count: 0 });
});
describe('Build workspace', () => {
  it('uses the main activity screen with keyboard Build navigation and project scope', async () => {
    mount('/products/product/projects/project/build');
    const tab = screen.getByRole('tab', { name: 'Delivery flows' });
    tab.focus(); await userEvent.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Agent activity' })).toHaveFocus();
    expect(await screen.findByRole('heading', { name: 'Agent Activity' })).toBeVisible();
    expect(await screen.findByText('Implement checkout')).toBeVisible();
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toBeVisible();
    expect(screen.queryByRole('tablist', { name: 'View scope' })).not.toBeInTheDocument();
    expect(screen.queryByText('View my Tasks')).not.toBeInTheDocument();
    expect(getMyInvocations).toHaveBeenCalledWith(expect.objectContaining({ page_size: 20 }), scope);
    screen.getByRole('tab', { name: 'Agent activity' }).focus(); await userEvent.keyboard('{Home}');
    expect(screen.getByText('Delivery flows for project')).toBeVisible();
  });

  it('opens the same run workspace and fetches scoped details', async () => {
    mount(); await userEvent.click(await screen.findByText('Implement checkout'));
    expect(await screen.findByRole('dialog', { name: 'Run workspace' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Live activity', exact: true })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Transcript & run record' })).toBeVisible();
    await waitFor(() => expect(getMyInvocationDetail).toHaveBeenCalledWith('run-one', scope));
    expect(screen.getByText(/Shared read access/)).toBeVisible();
  });

  it('filters and groups only the project endpoint', async () => {
    mount(); await screen.findByText('Implement checkout');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Filter by status' }), 'failed');
    await waitFor(() => expect(getMyInvocations).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'failed' }), scope));
    await userEvent.click(screen.getByRole('tab', { name: 'By chain' }));
    await waitFor(() => expect(getMyChains).toHaveBeenCalledWith(expect.objectContaining({ status: 'failed' }), scope));
  });

  it('clears the previous project and open run when switching scope', async () => {
    const rendered = mount(); await userEvent.click(await screen.findByText('Implement checkout'));
    vi.mocked(getMyInvocations).mockReturnValue(new Promise(() => {}));
    rendered.rerender(rendered.wrap('second'));
    expect(screen.queryByText('Implement checkout')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(getMyInvocations).toHaveBeenLastCalledWith(expect.anything(), { ...scope, projectId: 'second' }));
  });

  it('removes rows and the open run after access is revoked', async () => {
    const { client } = mount(); await userEvent.click(await screen.findByText('Implement checkout'));
    await screen.findByRole('dialog');
    vi.mocked(getMyInvocations).mockRejectedValue(new Error('Project unavailable'));
    await act(async () => { await client.invalidateQueries({ queryKey: ['agent-activity'] }); });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.queryByText('Implement checkout')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Project unavailable');
  });
});
