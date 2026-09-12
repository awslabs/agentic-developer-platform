/**
 * The Budget & Spend period wiring — regression coverage for defect #4970
 * (implementation child #4973, design PR #4971).
 *
 * **This file exists because every other test in this surface passed on the broken
 * code.** The defect was one wire key: the two `/me/budget*` clients sent the selected
 * period as `period`, both routes declare `period_type` with no alias, FastAPI ignores
 * an unknown query parameter, and so Daily and Weekly were served the `"monthly"`
 * default with an HTTP 200. Nothing failed. The page rendered monthly money, monthly
 * dates and monthly runs under a Daily heading, next to a correctly-fetched daily
 * personal limit — a wrong number that looked exactly like a right one.
 *
 * Two properties of the existing suites let that through, and this file is built to
 * avoid both:
 *
 *  1. `__tests__/components/BudgetSpend.test.tsx` MOCKS `services/budgetSpend` and
 *     asserts the fetcher was *called with* `'daily'`. That assertion is true on the
 *     broken code — the argument was always right; the query string was not. So this
 *     file does **not** mock the services. The real `getMyBudget` / `getMyBudgetRuns`
 *     run, build a real query string, and MSW answers it.
 *  2. The MSW handlers used to return a fixed monthly body whatever was asked for, so
 *     even an unmocked service could not have caught it. They now read `period_type`
 *     and answer with that period's fixture, exactly as the routes do — including
 *     resolving the `"monthly"` default when the key is absent, which is what
 *     reproduces the defect rather than erroring on it.
 *
 * The assertions are therefore on RENDERED OUTPUT per tab, not on call arguments:
 * headline spend, the date window, the direct-use and per-workspace breakdowns, the
 * personal-limit denominator and the runs drill-down. Fixtures differ from each other
 * in both dates AND money, so a client that fetched the right window but rendered
 * another period's body fails too.
 *
 * **The gate (design §4.3):** reverting `services/budgetSpend.ts` to `{ period }` must
 * make the period-wiring specs below fail. A test that passes either way has not
 * covered this defect. Evidence of both runs is on the PR.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { server } from '@/mocks/server';
import BudgetSpend from '@/pages/BudgetSpend';
import { mockBudgetEnvelopeFor, mockBudgetRunsFor, mockPersonCapFor } from '@/mocks/data/budgetSpend';
import type { BudgetPeriodType } from '@/types/budget';

// Only the auth token is stubbed — the services under test are deliberately REAL, and
// the page's own data all arrives through MSW.
vi.mock('@/services/auth', () => ({
  getAccessToken: () => 'test-token',
}));

function renderPage() {
  // `retry: false` so a thrown response-period guard surfaces immediately rather than
  // after React Query's default backoff.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <BudgetSpend />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * Open a drill-down by clicking its `<summary>`.
 *
 * The `data-testid` sits on the `<details>` element, but clicking that does not toggle
 * it — only the summary does. This matters beyond ergonomics for the runs pane: its
 * query is `enabled: runsOpen`, driven by the real `toggle` event, so a click that fails
 * to open it leaves the fetch un-fired and the pane empty. (Content inside a CLOSED
 * `<details>` is still in the DOM, so a query-by-testid assertion can pass without the
 * pane ever having been opened — which is why the runs assertions are on rows that only
 * exist once the fetch has actually run.)
 */
async function openDrillDown(user: ReturnType<typeof userEvent.setup>, testId: string) {
  const summary = screen.getByTestId(testId).querySelector('summary');
  if (!summary) throw new Error(`Drill-down ${testId} has no <summary> to open`);
  await user.click(summary);
}

/** Switch tabs and wait for that period's window to be on screen. */
async function selectPeriod(user: ReturnType<typeof userEvent.setup>, period: BudgetPeriodType) {
  await user.click(screen.getByTestId(`period-option-${period}`));
  const expected = mockBudgetEnvelopeFor(period).period;
  await waitFor(() => {
    expect(screen.getByText(`${expected.period_start} to ${expected.period_end}`)).toBeInTheDocument();
  });
}

beforeEach(() => {
  sessionStorage.setItem('access_token', 'test-token');
});

describe('Budget & Spend renders the selected period, not the monthly default', () => {
  // Monthly is the page's initial state, so it is asserted without a click; the other
  // two are the tabs the defect broke.
  it.each(['daily', 'weekly', 'monthly'] as const)('shows the %s period end to end', async (period) => {
    const user = userEvent.setup();
    const envelope = mockBudgetEnvelopeFor(period);
    const cap = mockPersonCapFor(period);
    const runs = mockBudgetRunsFor(period);

    renderPage();

    if (period === 'monthly') {
      await waitFor(() => {
        expect(screen.getByText(`${envelope.period.period_start} to ${envelope.period.period_end}`)).toBeInTheDocument();
      });
    } else {
      await selectPeriod(user, period);
    }

    // 1. The headline figure — the person's cross-org total for THIS period. On the
    //    broken code every tab showed the monthly $827.85.
    await waitFor(() => {
      expect(screen.getByTestId('my-spend-amount')).toHaveTextContent(formatted(envelope.person_envelope!.spend_usd));
    });

    // 2. The personal-limit denominator. This client was always correct, so on the
    //    broken code it was the ONE figure that changed with the tab — which is how the
    //    page came to show a daily limit under a monthly numerator. Here they must agree.
    expect(screen.getByTestId('my-spend-limit')).toHaveTextContent(formatted(cap.cap_usd!));

    // 3. The per-workspace breakdown, inside its drill-down.
    await openDrillDown(user, 'my-spend-drilldown-orgs');
    const orgRows = await screen.findAllByTestId('per-org-row');
    expect(orgRows).toHaveLength(envelope.per_org!.length);
    expect(within(orgRows[0]).getByTestId('per-org-spend')).toHaveTextContent(formatted(envelope.per_org![0].cloud_spend_usd));
    expect(within(orgRows[0]).getByTestId('per-org-direct-spend')).toHaveTextContent(formatted(envelope.per_org![0].direct_spend_usd));

    // 4. Direct use & other capped lines — the second breakdown, from the same envelope.
    await openDrillDown(user, 'my-spend-drilldown-lines');
    const lineRows = await screen.findAllByTestId('budget-line-row');
    expect(lineRows).toHaveLength(envelope.lines.length);
    const directRow = lineRows.find((row) => row.dataset.source === 'direct')!;
    expect(directRow).toHaveTextContent(formatted(envelope.lines[0].spend_usd));

    // 5. The runs drill-down, which is the SECOND fixed client and fires lazily on first
    //    expand. The row COUNT is what distinguishes the periods here (a longer window
    //    contains the shorter one's runs), so it is the assertion — the table renders no
    //    `run_id` column to check instead. On the broken code all three tabs listed the
    //    monthly page's three runs.
    await openDrillDown(user, 'my-spend-drilldown-runs');
    await waitFor(() => {
      expect(screen.getAllByTestId('budget-run-row')).toHaveLength(runs.items.length);
    });
    // The page-scoped subtotal caption, which counts this period's page and not the period.
    expect(screen.getByTestId('my-spend-drilldown-runs')).toHaveTextContent(`Subtotal for these ${runs.total_run_count} runs`);
  });

  it('asks for each period under the wire key the routes declare', async () => {
    // The request-level companion to the rendering specs above: whatever the page shows,
    // the query string that left the browser must carry `period_type` and must not carry
    // the obsolete `period` key. Recorded through a pass-through handler so the real
    // period-aware fixtures still answer.
    const seen: Array<{ path: string; params: URLSearchParams }> = [];
    const record = ({ request }: { request: Request }) => {
      const url = new URL(request.url);
      seen.push({ path: url.pathname, params: url.searchParams });
      return undefined;
    };
    server.events.on('request:start', ({ request }) => record({ request }));

    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('my-spend-amount')).toBeInTheDocument());
    await selectPeriod(user, 'daily');
    await openDrillDown(user, 'my-spend-drilldown-runs');
    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(mockBudgetRunsFor('daily').items.length));

    const budgetCalls = seen.filter((call) => call.path === '/api/me/budget');
    const runsCalls = seen.filter((call) => call.path === '/api/me/budget/runs');

    expect(budgetCalls.some((call) => call.params.get('period_type') === 'daily')).toBe(true);
    expect(runsCalls.some((call) => call.params.get('period_type') === 'daily')).toBe(true);
    // The defect's key, on every call this page made.
    expect([...budgetCalls, ...runsCalls].every((call) => !call.params.has('period'))).toBe(true);
  });

  it('does not show a previous period\'s figures while the next one loads', async () => {
    // The acceptance criterion behind the fix: a tab switch must never leave the old
    // period's money on screen under the new tab's heading. No `placeholderData` or
    // `keepPreviousData` is set in this surface, so the page falls to its skeleton — and
    // this pins that, because adding either option would silently reintroduce exactly
    // the confusion #4970 was about.
    const monthly = mockBudgetEnvelopeFor('monthly');
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId('my-spend-amount')).toHaveTextContent(formatted(monthly.person_envelope!.spend_usd));
    });

    // Hold the daily response open so the intermediate state is observable.
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get('/api/me/budget', async ({ request }) => {
        await held;
        return HttpResponse.json(mockBudgetEnvelopeFor(new URL(request.url).searchParams.get('period_type') as BudgetPeriodType));
      }),
    );

    await user.click(screen.getByTestId('period-option-daily'));

    await waitFor(() => expect(screen.getByTestId('budget-loading')).toBeInTheDocument());
    expect(screen.queryByText(formatted(monthly.person_envelope!.spend_usd))).not.toBeInTheDocument();

    release();
    await waitFor(() => {
      expect(screen.getByTestId('my-spend-amount')).toHaveTextContent(formatted(mockBudgetEnvelopeFor('daily').person_envelope!.spend_usd));
    });
  });

  it('resolves a rapid daily→weekly→daily switch to the period left selected', async () => {
    // Each period has its own React Query cache entry (`['myBudget', period]`), so a
    // slow response cannot land in another period's entry. Pinned here because the
    // failure mode — a late response overwriting a newer tab's figures — is invisible in
    // a suite where every fetch resolves instantly with the same body.
    const user = userEvent.setup();

    // Daily answers slowly, weekly immediately: the daily request is still in flight
    // when the second daily click re-selects it.
    server.use(
      http.get('/api/me/budget', async ({ request }) => {
        const period = new URL(request.url).searchParams.get('period_type') as BudgetPeriodType;
        if (period === 'daily') await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json(mockBudgetEnvelopeFor(period));
      }),
    );

    renderPage();
    await waitFor(() => expect(screen.getByTestId('my-spend-amount')).toBeInTheDocument());

    await user.click(screen.getByTestId('period-option-daily'));
    await user.click(screen.getByTestId('period-option-weekly'));
    await user.click(screen.getByTestId('period-option-daily'));

    const daily = mockBudgetEnvelopeFor('daily');
    await waitFor(() => {
      expect(screen.getByText(`${daily.period.period_start} to ${daily.period.period_end}`)).toBeInTheDocument();
      expect(screen.getByTestId('my-spend-amount')).toHaveTextContent(formatted(daily.person_envelope!.spend_usd));
    });

    // And no trace of the period that was passed through on the way.
    const weekly = mockBudgetEnvelopeFor('weekly');
    expect(screen.queryByText(`${weekly.period.period_start} to ${weekly.period.period_end}`)).not.toBeInTheDocument();
  });
});

describe('a response describing the wrong period reaches the error state, not the screen', () => {
  it('shows the error affordance rather than a figure when the response period mismatches', async () => {
    // D2 of the approved design, at the page level: the guard's whole purpose is that a
    // wrong-period body becomes a VISIBLE failure instead of a confident figure. The
    // copy asserted here is the page's existing error state, which explicitly refuses to
    // let the failure read as zero spend.
    server.use(http.get('/api/me/budget', () => HttpResponse.json(mockBudgetEnvelopeFor('monthly'))));

    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByTestId('my-spend-amount')).toBeInTheDocument());

    await user.click(screen.getByTestId('period-option-daily'));

    await waitFor(() => {
      expect(screen.getByTestId('budget-error')).toBeInTheDocument();
    });
    expect(screen.getByTestId('budget-error')).toHaveTextContent(/not a statement that your spend is zero/i);
    // The monthly figures that arrived must not be rendered under the daily tab — the
    // mismatch is refused, not displayed.
    expect(screen.queryByText(formatted(mockBudgetEnvelopeFor('monthly').person_envelope!.spend_usd))).not.toBeInTheDocument();
    // And the figure is absent rather than zero: "$0.00" here would be the original sin.
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('surfaces a wrong-period runs page as the runs error, not as an empty list', async () => {
    // An empty list would read as "nothing ran in this period", which is a claim we
    // cannot support when the response we got described a different period.
    server.use(http.get('/api/me/budget/runs', () => HttpResponse.json(mockBudgetRunsFor('monthly'))));

    const user = userEvent.setup();
    renderPage();
    await selectPeriod(user, 'weekly');

    await openDrillDown(user, 'my-spend-drilldown-runs');

    await waitFor(() => {
      expect(screen.getByTestId('runs-error')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('runs-empty')).not.toBeInTheDocument();
    expect(screen.queryAllByTestId('budget-run-row')).toHaveLength(0);
  });
});

/**
 * The screen's money formatting, applied to a wire string so assertions compare what a
 * user actually reads.
 *
 * Duplicated deliberately rather than imported from the component's helper: a test that
 * formats with the same function it is checking would agree with that function even if
 * both were wrong. This mirrors `formatWireMoney`'s contract — 2dp, thousands separated.
 */
function formatted(wireMoney: string): string {
  return `$${Number(wireMoney).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
