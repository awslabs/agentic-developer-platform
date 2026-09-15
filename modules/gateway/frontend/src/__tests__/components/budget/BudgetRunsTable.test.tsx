import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BudgetRunsTable } from '@/components/budget/BudgetRunsTable';
import { mockBudgetRuns } from '@/mocks/data/budgetSpend';

describe('BudgetRunsTable — states', () => {
  it('renders a loading skeleton while the runs are in flight', () => {
    render(<BudgetRunsTable data={undefined} isLoading />);
    expect(screen.getByTestId('runs-loading')).toBeInTheDocument();
  });

  it('reports a failed read as a failure, not as an empty list', () => {
    // "We could not look" and "you ran nothing" are opposite claims. An error state
    // that renders as an empty table asserts the second while only knowing the first.
    render(<BudgetRunsTable data={undefined} error={new Error('boom')} />);

    expect(screen.getByTestId('runs-error')).toBeInTheDocument();
    expect(screen.getByTestId('runs-error').textContent).toMatch(/not a statement that you have none/i);
    expect(screen.queryByTestId('runs-empty')).not.toBeInTheDocument();
  });

  it('renders an empty state when the period genuinely has no runs', () => {
    render(
      <BudgetRunsTable
        data={{ ...mockBudgetRuns, items: [], total_run_count: 0, subtotal: { status: 'none_incurred', amount_usd: '0.000000', partial: false } }}
      />,
    );
    expect(screen.getByTestId('runs-empty')).toBeInTheDocument();
  });

  it('says cloud runs are missing rather than absent when identity is unresolved', () => {
    render(<BudgetRunsTable data={{ ...mockBudgetRuns, identity_status: 'unresolved' }} />);

    expect(screen.getByTestId('runs-identity-unresolved').textContent).toMatch(/not a statement that you have none/i);
  });

  it('offers pagination only when the response carries a cursor', async () => {
    const onLoadMore = vi.fn();
    const { rerender } = render(<BudgetRunsTable data={{ ...mockBudgetRuns, next_cursor: null }} onLoadMore={onLoadMore} />);
    expect(screen.queryByRole('button', { name: /Load more runs/i })).not.toBeInTheDocument();

    rerender(<BudgetRunsTable data={{ ...mockBudgetRuns, next_cursor: 'cursor-2' }} onLoadMore={onLoadMore} />);
    await userEvent.click(screen.getByRole('button', { name: /Load more runs/i }));
    expect(onLoadMore).toHaveBeenCalledOnce();
  });

  it('renders a run with no persona or start time without inventing values', () => {
    render(
      <BudgetRunsTable
        data={{ ...mockBudgetRuns, items: [{ ...mockBudgetRuns.items[0], persona: null, started_at: null, status: null }], total_run_count: 1 }}
      />,
    );

    const row = screen.getByTestId('budget-run-row');
    expect(within(row).getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('distinguishes direct from cloud attribution', () => {
    render(<BudgetRunsTable data={mockBudgetRuns} />);

    const badges = screen.getAllByTestId('run-attribution');
    expect(badges.map((b) => b.getAttribute('data-attribution'))).toEqual(['cloud', 'cloud', 'direct']);
  });
});
