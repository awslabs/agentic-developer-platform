import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import BudgetSpend from '@/pages/BudgetSpend';
import { getMonthlySpend } from '@/services/budgetOverview';
import { getMySelection } from '@/services/bedrockRoutingSelf';
import { getMyPersonCap } from '@/services/personCap';
import { getMyBudget } from '@/services/budgetSpend';
import { mockMonthlySpend } from '@/mocks/data/budgetOverview';
import { mockBudgetEnvelope, mockPersonCapEnforcing } from '@/mocks/data/budgetSpend';

vi.mock('@/components/FeatureGate', () => ({ FeatureGate: ({ children }: { children: React.ReactNode }) => children }));
vi.mock('@/services/budgetOverview', () => ({ getMonthlySpend: vi.fn() }));
vi.mock('@/services/bedrockRoutingSelf', () => ({ getMySelection: vi.fn() }));
vi.mock('@/services/personCap', () => ({ getMyPersonCap: vi.fn() }));
vi.mock('@/services/budgetSpend', () => ({ getMyBudget: vi.fn() }));
vi.mock('@/pages/BudgetManagement', () => ({ BudgetManagement: () => <div>Additional budget controls</div> }));
vi.mock('@/components/budget/BudgetHierarchy', () => ({ BudgetHierarchy: () => <div>Budget hierarchy</div> }));
const roles = vi.hoisted(() => ({ platform: false, org: false }));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ isPlatformAdmin: () => roles.platform, isOrgAdmin: () => roles.org, canViewBudgets: () => true }) }));

function show(path = '/budget') {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[path]}><BudgetSpend /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => {
  roles.platform = false; roles.org = false;
  vi.mocked(getMonthlySpend).mockResolvedValue(mockMonthlySpend);
  vi.mocked(getMyPersonCap).mockResolvedValue({ ...mockPersonCapEnforcing, cap_usd: '500.00', source: 'team_default', source_label: 'Team default · Platform' });
  vi.mocked(getMyBudget).mockResolvedValue(mockBudgetEnvelope);
  vi.mocked(getMySelection).mockResolvedValue({ effective: { rung: 'team', destination_label: 'Development', account_id: '123456789012' } } as Awaited<ReturnType<typeof getMySelection>>);
});

describe('Monthly Budget & Spend', () => {
  it('shows one combined budget, both components and daily totals including zero days', async () => {
    show();
    expect(await screen.findByText('$247.50 spent')).toBeInTheDocument();
    expect(screen.getByText('of $500.00 this month')).toBeInTheDocument();
    expect(screen.getByText('$252.50 remaining')).toBeInTheDocument();
    expect(screen.getByText('123456789012')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Manage budgets' })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Budget period' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByText('Usage breakdown'));
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(16);
    expect(within(table).getByText('In progress')).toBeInTheDocument();
    expect(within(table).getAllByText('$0.00')).toHaveLength(39);
    expect(within(table).getAllByText('$247.50')).toHaveLength(2);
  });
  it('keeps the account and budget visible when spend fails', async () => {
    vi.mocked(getMonthlySpend).mockRejectedValue(new Error('offline'));
    show();
    expect(await screen.findByText('Spend unavailable')).toBeInTheDocument();
    expect(screen.getByText('123456789012')).toBeInTheDocument();
    expect(screen.getByText('of $500.00 this month')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });
  it('keeps spend visible when routing and budget fail', async () => {
    vi.mocked(getMySelection).mockRejectedValue(new Error('offline'));
    vi.mocked(getMyPersonCap).mockRejectedValue(new Error('offline'));
    show();
    expect(await screen.findByText('Budget unavailable')).toBeInTheDocument();
    expect(screen.getByText('Account details unavailable')).toBeInTheDocument();
    expect(screen.getByText('$247.50 spent')).toBeInTheDocument();
    expect(screen.queryByText('No monthly budget set')).not.toBeInTheDocument();
  });
  it('distinguishes incomplete daily data from days without usage', async () => {
    vi.mocked(getMonthlySpend).mockResolvedValue({ ...mockMonthlySpend, daily_complete: false, days: [] });
    show();
    await screen.findByText('$247.50 spent');
    await userEvent.click(screen.getByText('Usage breakdown'));
    expect(screen.getByText(/Daily spend is not yet reconciled/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
  it('reports actual overage and accessible progress beyond 100%', async () => {
    vi.mocked(getMyPersonCap).mockResolvedValue({ ...mockPersonCapEnforcing, cap_usd: '100.00' });
    show();
    expect(await screen.findByText('$147.50 over budget')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '247.5% of monthly budget used');
  });
  it('labels advisory budgets and does not draw an enforced progress bar', async () => {
    vi.mocked(getMyPersonCap).mockResolvedValue({ ...mockPersonCapEnforcing, enforcement_mode: 'soft' });
    show();
    expect(await screen.findByText('This monthly budget is advisory.')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });
  it('falls back to My spend for an unauthorized management deep link', async () => {
    show('/budget?view=manage');
    expect(await screen.findByText('$247.50 spent')).toBeInTheDocument();
    expect(screen.queryByText('Budget hierarchy')).not.toBeInTheDocument();
  });
  it('keeps a single title when an admin switches to the hierarchy', async () => {
    roles.platform = true;
    show();
    await screen.findByText('$247.50 spent');
    await userEvent.click(screen.getByRole('button', { name: 'Manage budgets' }));
    expect(screen.getByText('Budget hierarchy')).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.queryByText('$247.50 spent')).not.toBeInTheDocument();
  });
  it('does not mount global hierarchy or spend report for org admins', () => {
    roles.org = true;
    show('/budget?view=manage');
    expect(screen.queryByText('Budget hierarchy')).not.toBeInTheDocument();
    expect(screen.getByText(/managed by a platform admin because/)).toBeInTheDocument();
  });
});
