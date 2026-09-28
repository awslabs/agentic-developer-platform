/** Real HTTP contracts remain important for the additional daily/weekly restrictions. */
import { expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { server } from '@/mocks/server';
import { MonthlySpendView } from '@/components/budget/MonthlySpend';
import { getMyBudget, getMyBudgetRuns } from '@/services/budgetSpend';
import { mockBudgetEnvelopeFor, mockBudgetRunsFor, mockPersonCapFor } from '@/mocks/data/budgetSpend';
import { mockMonthlySpend } from '@/mocks/data/budgetOverview';
import type { BudgetPeriodType } from '@/types/budget';
vi.mock('@/services/auth', () => ({ getAccessToken: () => 'test-token' }));

it('fetches restrictions with period_type while the summary stays monthly', async () => {
  const periods: string[] = [];
  server.use(
    http.get('*/me/budget/monthly-spend', () => HttpResponse.json(mockMonthlySpend)),
    http.get('*/me/bedrock-routing/selection', () => HttpResponse.json({ effective: { rung: 'platform', account_id: null } })),
    http.get('*/me/budget/person-cap', ({ request }) => HttpResponse.json(mockPersonCapFor(new URL(request.url).searchParams.get('period_type') as BudgetPeriodType || 'monthly'))),
    http.get('*/me/budget', ({ request }) => { const period = new URL(request.url).searchParams.get('period_type') || 'monthly'; periods.push(period); return HttpResponse.json(mockBudgetEnvelopeFor(period as BudgetPeriodType)); }),
  );
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MonthlySpendView /></QueryClientProvider>);
  await screen.findByText('$247.50 spent');
  await userEvent.click(screen.getByText('Budget details'));
  await userEvent.click(screen.getByText('Additional restrictions'));
  await waitFor(() => expect(new Set(periods)).toEqual(new Set(['monthly', 'weekly', 'daily'])));
  expect(screen.getByText('$247.50 spent')).toBeInTheDocument();
});

it('refuses a wrong-period budget or runs response', async () => {
  server.use(http.get('*/me/budget', () => HttpResponse.json(mockBudgetEnvelopeFor('monthly'))),
    http.get('*/me/budget/runs', () => HttpResponse.json(mockBudgetRunsFor('monthly'))));
  await expect(getMyBudget('daily')).rejects.toThrow();
  await expect(getMyBudgetRuns({ period: 'weekly' })).rejects.toThrow();
});

it('shows new cloud agent spend while the monthly page stays open', async () => {
  let cloud = '100.00';
  let budgetReads = 0;
  server.use(
    http.get('*/me/budget/monthly-spend', () => HttpResponse.json({ ...mockMonthlySpend, totals: {
      direct_usd: '20.00', cloud_usd: cloud, total_usd: String(20 + Number(cloud)),
    } })),
    http.get('*/me/bedrock-routing/selection', () => HttpResponse.json({ effective: { rung: 'platform', account_id: null } })),
    http.get('*/me/budget/person-cap', () => HttpResponse.json(mockPersonCapFor('monthly'))),
    http.get('*/me/budget', () => { budgetReads++; return HttpResponse.json(mockBudgetEnvelopeFor('monthly')); }),
  );
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><MonthlySpendView /></QueryClientProvider>);
  try {
    await screen.findByText('$120.00 spent');
    await waitFor(() => expect(budgetReads).toBe(1));
    cloud = '125.00';
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    await screen.findByText('$145.00 spent');
    expect(budgetReads).toBe(2);
  } finally {
    view.unmount();
    client.clear();
    vi.useRealTimers();
  }
});
