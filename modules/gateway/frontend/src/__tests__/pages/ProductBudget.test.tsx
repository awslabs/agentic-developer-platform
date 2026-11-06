import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import ProductBudget from '@/pages/products/ProductBudget';
import { getProjectBudget, setProjectBudget, type ProjectBudget as Budget } from '@/services/products';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'member', orgId: 'tenant' } }) }));
vi.mock('@/services/products', () => ({ productPath: (product: string, project: string) => `/products/${product}/projects/${project}`, getProjectBudget: vi.fn(), setProjectBudget: vi.fn() }));
const base: Budget = { product_id: 'product', project_id: 'base', project_name: 'Base', is_base: true,
  budget_usd: 100, spent_usd: 12, reserved_usd: 3, available_usd: 85, accounting_status: 'pending', can_manage: true,
  flows: [{ flow_id: 'flow', flow_name: 'Launch checkout', spent_usd: 12, run_count: 2 }] };
function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><ProductBudget productId="product" projectId="base" /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { vi.clearAllMocks(); vi.mocked(getProjectBudget).mockResolvedValue(base); });
it('shows shared balance and saves the selected Project budget', async () => {
  vi.mocked(setProjectBudget).mockResolvedValue({ ...base, budget_usd: 200, available_usd: 185 });
  const user = userEvent.setup(); show();
  expect(await screen.findByText('$85.00')).toBeVisible();
  expect(screen.getByText(/New Product work uses this Project by default/)).toBeVisible();
  expect(screen.getByRole('link', { name: 'Launch checkout' })).toHaveAttribute('href', '/flows/flow');
  expect(screen.getByRole('link', { name: 'Create a flow' })).toHaveAttribute('href', '/flows/new?product=product&project=base');
  await user.clear(screen.getByRole('spinbutton')); await user.type(screen.getByRole('spinbutton'), '200');
  await user.click(screen.getByRole('button', { name: 'Save budget' }));
  await waitFor(() => expect(setProjectBudget).toHaveBeenCalledWith('base', '200'));
  expect(await screen.findByText('$185.00')).toBeVisible();
});
it('does not represent unavailable accounting as zero or permit a member to edit', async () => {
  vi.mocked(getProjectBudget).mockResolvedValue({ ...base, can_manage: false, spent_usd: null, reserved_usd: null, available_usd: null, accounting_status: 'unavailable' });
  show();
  expect(await screen.findByRole('alert')).toHaveTextContent('Spend is temporarily unavailable');
  expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
  expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  expect(screen.getAllByText('Unavailable')).toHaveLength(3);
});
