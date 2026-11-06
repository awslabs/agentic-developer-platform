import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ProductInsights from '@/pages/products/ProductInsights';
import { getBusinessContextEvidence } from '@/services/engineeringInsights';
import { listSourceBindings } from '@/services/products';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'owner', orgId: 'tenant' } }) }));
vi.mock('@/services/engineeringInsights', () => ({ getBusinessContextEvidence: vi.fn() }));
vi.mock('@/services/products', async original => ({ ...await original<typeof import('@/services/products')>(), listSourceBindings: vi.fn() }));
const evidence = { schema_version: 'results/1.0', view: 'business_context', product_id: 'product', project_id: 'project', as_of: new Date().toISOString(), dataset_version: 'dataset-one', mapping_versions: ['mapping-one'], coverage: 'complete', work_count: 7, cost_by_currency: { USD: '18.50' }, contexts: [{ subject_ref: { record_id: 'story-one', record_type: 'story', record_revision: '4' } }], next_cursor: null } as const;
function mount() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><ProductInsights productId="product" projectId="project" /></MemoryRouter></QueryClientProvider>); }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(listSourceBindings).mockResolvedValue({ items: [] }); vi.mocked(getBusinessContextEvidence).mockResolvedValue(evidence as never); });

describe('Product Insights', () => {
  it('does not collect data on navigation, invent metrics or confuse PR cycle time with production delivery', async () => {
    mount();
    expect(getBusinessContextEvidence).not.toHaveBeenCalled();
    expect(listSourceBindings).not.toHaveBeenCalled();
    expect(screen.queryByText(/1.8 days|18 of 24|Example/)).not.toBeInTheDocument();
    await userEvent.click(screen.getAllByRole('button', { name: 'View report' })[0]);
    const dialog = within(screen.getByRole('dialog'));
    expect(dialog.getAllByText('Awaiting metric integration')).toHaveLength(4);
    await userEvent.click(dialog.getAllByRole('button', { name: 'Definition & evidence' })[0]);
    expect(dialog.getByText(/does not measure time to production/)).toBeVisible();
    await userEvent.click(dialog.getByRole('button', { name: '← Back to report' }));
    expect(dialog.getAllByRole('button', { name: 'Definition & evidence' })).toHaveLength(4);
  });

  it.each([0, 1, 2])('supports every definition and returning to report %i', async index => {
    mount(); await userEvent.click(screen.getAllByRole('button', { name: 'View report' })[index]);
    const count = screen.getAllByRole('button', { name: 'Definition & evidence' }).length;
    for (let metric = 0; metric < count; metric++) {
      await userEvent.click(screen.getAllByRole('button', { name: 'Definition & evidence' })[metric]);
      expect(screen.getByText('Freshness & coverage')).toBeVisible();
      await userEvent.click(screen.getByRole('button', { name: '← Back to report' }));
    }
    await userEvent.keyboard('{Escape}'); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('loads saved source bindings on demand and keeps collection status distinct', async () => {
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [{ id: 'binding', project_id: 'project', status: 'healthy', selection: { provider: 'github', kind: 'repository', namespace: 'team', native_id: 'repo' } }] });
    mount(); await userEvent.click(screen.getByRole('button', { name: 'Data sources & coverage' }));
    expect(await screen.findByText(/Analytics collection not reported/)).toBeVisible();
    expect(listSourceBindings).toHaveBeenCalledWith('product', 'project');
    expect(screen.getByRole('link', { name: 'Review product connections' })).toHaveAttribute('href', '/products/product/projects/project#product-connections');
  });

  it('reads scoped published evidence without interpreting mapped work as shipped stories', async () => {
    mount(); await userEvent.click(screen.getAllByRole('button', { name: 'View report' })[2]);
    await userEvent.click(screen.getByRole('button', { name: 'Inspect published business-context evidence' }));
    expect(await screen.findByText('dataset-one')).toBeVisible();
    expect(screen.getByText('Mapped work items')).toBeVisible();
    expect(screen.getByText('18.50 USD')).toBeVisible();
    expect(getBusinessContextEvidence).toHaveBeenCalledWith('product', 'project', expect.any(String), undefined, expect.any(AbortSignal));
    expect(screen.getByRole('button', { name: 'Next records' })).toBeDisabled();
  });

  it.each([[404, 'current access'], [409, 'reconcile and republish'], [501, 'not enabled'], [503, 'Retry']])('provides an actionable %i without displaying cached evidence', async (status, message) => {
    vi.mocked(getBusinessContextEvidence).mockRejectedValue({ status });
    mount(); await userEvent.click(screen.getAllByRole('button', { name: 'View report' })[2]);
    await userEvent.click(screen.getByRole('button', { name: 'Inspect published business-context evidence' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(String(message));
    expect(screen.queryByText('dataset-one')).not.toBeInTheDocument();
    vi.mocked(getBusinessContextEvidence).mockResolvedValue(evidence as never);
    await userEvent.click(screen.getByRole('button', { name: 'Retry evidence' }));
    await waitFor(() => expect(screen.getByText('dataset-one')).toBeVisible());
  });
});
