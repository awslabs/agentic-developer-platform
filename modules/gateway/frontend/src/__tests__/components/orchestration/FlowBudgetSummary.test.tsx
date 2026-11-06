import { beforeEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { FlowBudgetSummary, StoryRunBudgets } from '@/components/orchestration/FlowBudgetSummary';
import { getFlowBudget, getNodeBudget, type FlowBudget } from '@/services/flowBudget';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { orgId: 'tenant-a' } }) }));
vi.mock('@/services/flowBudget', () => ({ getFlowBudget: vi.fn(), getNodeBudget: vi.fn() }));
const budget: FlowBudget = {
  flow_id: 'flow-a', observed_at: '2026-10-27T12:00:00Z', enforcement_enabled: true, accounting_incomplete: false,
  run_limit_usd: '100', concurrency: 5, policy_status: 'accepted', proposed: null,
  balance: { limit_usd: '2000', spent_usd: '450.25', reserved_usd: '30.25', remaining_usd: '1519.50' },
};
function mount(content: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>;
  return { client, ...render(content, { wrapper }) };
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getFlowBudget).mockResolvedValue(budget);
});

it('shows the effective flow and per-run limits beside spend and reservations', async () => {
  mount(<FlowBudgetSummary flowId="flow-a" active />);
  const panel = screen.getByRole('region', { name: 'Delivery flow budget' });
  expect(await within(panel).findByText('$2000.00')).toBeVisible();
  for (const value of ['$450.25', '$30.25', '$1519.50', '$100.00', '5']) expect(within(panel).getByText(value)).toBeVisible();
  expect(within(panel).getByText('Enforcement on')).toBeVisible();
});

it('separates recovery uncertainty from model requests without adding the hold twice', async () => {
  vi.mocked(getFlowBudget).mockResolvedValue({ ...budget, balance: {
    ...budget.balance, reserved_usd: '530.25', request_reserved_usd: '30.25', recovery_hold_usd: '500', remaining_usd: '1019.50',
  } });
  mount(<FlowBudgetSummary flowId="flow-a" active />);
  expect(await screen.findByText('$530.25')).toBeVisible();
  const breakdown = screen.getByLabelText('Reservation breakdown');
  expect(within(breakdown).getByText('$30.25')).toBeVisible();
  expect(within(breakdown).getByText('$500.00')).toBeVisible();
  expect(screen.getByText('$1019.50')).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent('not recorded spend or active work');
});

it('does not invent a zero historical hold when an older server cannot classify it', async () => {
  mount(<FlowBudgetSummary flowId="flow-a" active />);
  expect(await screen.findByText('$30.25')).toBeVisible();
  expect(screen.queryByLabelText('Reservation breakdown')).not.toBeInTheDocument();
  expect(screen.queryByText(/Historical usage could not/)).not.toBeInTheDocument();
});

it('shows an unaccepted plan’s proposed limits as proposed, never as an in-force balance', async () => {
  vi.mocked(getFlowBudget).mockResolvedValue({ ...budget, run_limit_usd: null, concurrency: null, policy_status: 'proposed',
    balance: { limit_usd: null, spent_usd: '0', reserved_usd: null, remaining_usd: null },
    proposed: { limit_usd: '1000', concurrency: 2, attempts_per_node: 3, wall_clock_seconds: 1209600, expires_at: '2026-11-16T20:31:17Z', plan_version: 1, plan_hash: '3146e7433ead74960cdfcd1c36d3f6638871bc435228c48a3ddef84604734853' } });
  mount(<FlowBudgetSummary flowId="flow-a" active />);
  const panel = screen.getByRole('region', { name: 'Delivery flow budget' });
  expect(await within(panel).findByText('Proposed · awaiting acceptance')).toBeVisible();
  expect(within(panel).getByLabelText('Proposed budget')).toBeVisible();
  for (const value of ['$1000.00', '2', '3', '14 days']) expect(within(panel).getByText(value)).toBeVisible();
  expect(within(panel).getByText(/No budget is in force/)).toBeVisible();
  expect(within(panel).getByText('3146e7433ead')).toBeVisible();
  expect(within(panel).queryByText('Flow budget')).not.toBeInTheDocument();
  expect(within(panel).queryByText('Remaining')).not.toBeInTheDocument();
  expect(within(panel).queryByText(/Enforcement (on|off)/)).not.toBeInTheDocument();
});

it('does not invent a zero balance for unknown accounting and explains off mode', async () => {
  vi.mocked(getFlowBudget).mockResolvedValue({ ...budget, enforcement_enabled: false, accounting_incomplete: true,
    balance: { ...budget.balance, reserved_usd: null, remaining_usd: null } });
  mount(<FlowBudgetSummary flowId="flow-a" active />);
  expect(await screen.findByText('Enforcement off')).toBeVisible();
  expect(screen.getAllByText('Unavailable')).toHaveLength(2);
  expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  expect(screen.getByText(/not currently enforced/)).toBeVisible();
});

it('replaces stale amounts with a retry notice when refresh fails', async () => {
  const { client } = mount(<FlowBudgetSummary flowId="flow-a" active />);
  expect(await screen.findByText('$1519.50')).toBeVisible();
  vi.mocked(getFlowBudget).mockRejectedValue(new Error('unavailable'));
  await act(async () => { await client.invalidateQueries({ queryKey: ['orchestration', 'flow-budget'] }); });
  expect(await screen.findByRole('button', { name: 'Retry budget' })).toBeVisible();
  expect(screen.queryByText('$1519.50')).not.toBeInTheDocument();
});

it('does not fetch budgets on inactive tabs or show another flow’s cached values', async () => {
  const view = mount(<FlowBudgetSummary flowId="flow-a" active={false} />);
  expect(getFlowBudget).not.toHaveBeenCalled();
  view.rerender(<FlowBudgetSummary flowId="flow-a" active />);
  expect(await screen.findByText('$2000.00')).toBeVisible();
  vi.mocked(getFlowBudget).mockImplementation(() => new Promise(() => {}));
  view.rerender(<FlowBudgetSummary flowId="flow-b" active />);
  expect(screen.queryByText('$2000.00')).not.toBeInTheDocument();
  expect(screen.getByText('Loading budget…')).toBeVisible();
});

it('loads each story’s run balances only when expanded and keeps absent counters unknown', async () => {
  vi.mocked(getNodeBudget).mockResolvedValue({ flow_id: 'flow-a', node_id: 'node-a', observed_at: budget.observed_at,
    enforcement_enabled: true, truncated: false,
    runs: [
      { run_id: 'review-run', persona: 'agent-codex-reviewer', last_call_at: budget.observed_at,
        balance: { limit_usd: '100', spent_usd: '41.22', reserved_usd: '8.33', remaining_usd: '50.45' } },
      { run_id: 'old-run', persona: null, last_call_at: budget.observed_at,
        balance: { limit_usd: '100', spent_usd: '20', reserved_usd: null, remaining_usd: null } },
    ] });
  mount(<StoryRunBudgets flowId="flow-a" nodeId="node-a" active />);
  expect(getNodeBudget).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Run budgets'));
  const table = await screen.findByRole('table', { name: 'Budget by agent run' });
  expect(within(table).getByText('$50.45')).toBeVisible();
  expect(within(table).getAllByText('Unavailable')).toHaveLength(2);
  expect(within(table).getByRole('link', { name: /codex-reviewer/ })).toHaveAttribute('href', '/activity?id=review-run');
  await waitFor(() => expect(getNodeBudget).toHaveBeenCalledTimes(1));
});
