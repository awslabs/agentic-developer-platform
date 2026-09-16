import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BudgetHierarchy } from '@/components/budget/BudgetHierarchy';
import { getBudgetHierarchy, getPeopleSpend } from '@/services/budgetOverview';
import { setPersonDefault, deletePersonDefault, setPersonCapFor } from '@/services/personCap';
import { mockBudgetHierarchy, mockPeopleSpend } from '@/mocks/data/budgetOverview';
vi.mock('@/services/budgetOverview', () => ({ getBudgetHierarchy: vi.fn(), getPeopleSpend: vi.fn() }));
vi.mock('@/services/personCap', () => ({ setPersonDefault: vi.fn(), deletePersonDefault: vi.fn(), setPersonCapFor: vi.fn(), deletePersonCapFor: vi.fn() }));
beforeEach(() => {
  vi.mocked(getBudgetHierarchy).mockResolvedValue(structuredClone(mockBudgetHierarchy));
  vi.mocked(getPeopleSpend).mockResolvedValue(mockPeopleSpend);
});
function show() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><BudgetHierarchy /></QueryClientProvider>); }
it('keeps teams inside their organization and edits the exact scope with a fallback preview', async () => {
  show();
  const button = await screen.findByRole('button', { name: 'Edit budget for Platform' });
  const org = screen.getByRole('button', { name: 'Engineering' }).closest('li')!;
  expect(within(org).getByRole('button', { name: 'Platform' })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Platform', exact: true }));
  expect(screen.getByRole('button', { name: 'Set budget for Alex Morgan' })).toBeInTheDocument();
  await userEvent.click(button);
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('Platform default → Engineering → Platform')).toBeInTheDocument();
  expect(within(dialog).getByText('$750.00')).toBeInTheDocument();
  const input = within(dialog).getByRole('spinbutton');
  await userEvent.clear(input); await userEvent.type(input, '1200.00');
  await userEvent.click(within(dialog).getByRole('button', { name: 'Save budget' }));
  await waitFor(() => expect(setPersonDefault).toHaveBeenCalledWith({ scope_type: 'team', org: 'engineering', team: 'platform' }, 'monthly', '1200'));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: 'Platform', exact: true })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('button', { name: 'Set budget for Alex Morgan' })).toBeInTheDocument();
});
it('uses deletion to inherit the parent instead of writing a zero budget', async () => {
  show();
  await userEvent.click(await screen.findByRole('button', { name: 'Edit budget for Engineering' }));
  expect(within(screen.getByRole('dialog')).getByText('$300.00')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Remove override' }));
  await waitFor(() => expect(deletePersonDefault).toHaveBeenCalledWith({ scope_type: 'org', org: 'engineering', team: undefined }, 'monthly'));
});
it('uses the server person anchor for an individual budget inside its team', async () => {
  show();
  await userEvent.click(await screen.findByRole('button', { name: 'Platform', exact: true }));
  await userEvent.click(screen.getByRole('button', { name: 'Set budget for Alex Morgan' }));
  expect(screen.getByText('Platform default → Engineering → Platform → Alex Morgan')).toBeInTheDocument();
  await userEvent.type(screen.getByRole('spinbutton'), '1000');
  await userEvent.click(screen.getByRole('button', { name: 'Save budget' }));
  await waitFor(() => expect(setPersonCapFor).toHaveBeenCalledWith('users:alex', 'monthly', '1000'));
});
it('keeps the editor open with the server error if a write fails', async () => {
  vi.mocked(setPersonDefault).mockRejectedValueOnce(new Error('Budget could not be saved'));
  show();
  await userEvent.click(await screen.findByRole('button', { name: 'Edit budget for Engineering' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save budget' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Budget could not be saved');
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});
it('paginates the person report and resets to page one on search', async () => {
  vi.mocked(getPeopleSpend).mockResolvedValue({ ...mockPeopleSpend, total: 26 });
  show();
  await userEvent.click(await screen.findByRole('button', { name: 'Next' }));
  await waitFor(() => expect(getPeopleSpend).toHaveBeenCalledWith(2, ''));
  await userEvent.type(screen.getByRole('searchbox'), 'Alex');
  await waitFor(() => expect(getPeopleSpend).toHaveBeenCalledWith(1, 'Alex'));
  expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
});
