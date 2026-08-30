/**
 * Tests for the Budget & Spend screen — Issue #4402 (U-5 of EPIC #4324).
 *
 * Covers the issue's numbered validation criteria: a member sees the nav item and their
 * own figures (1), exactly three period options with no RUN/CHAIN (2), `unknown` cost
 * rendering `—` rather than `$0.00` (5), the shadow-mode banner with no
 * "will be stopped" copy (6), flag off removing both route and nav (7), the freshness
 * affordance (8), and no `change=` prop on any spend tile (9).
 *
 * Fixtures come from `mocks/data/budgetSpend.ts`, transcribed from
 * `src/budget/schemas.py`. Writing them from the frontend type is what let #3675 ship a
 * dashboard whose mocks, tests and eval all validated fields the backend never sent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import BudgetSpend from '@/pages/BudgetSpend';
import { BudgetRunsTable } from '@/components/budget/BudgetRunsTable';
import { Navigation } from '@/components/Navigation';
import { FeatureGate } from '@/components/FeatureGate';
import { mockBudgetEnvelope, mockBudgetRuns, mockUncappedLine } from '@/mocks/data/budgetSpend';
import type { FeatureFlags } from '@/services/features';

vi.mock('@/services/budgetSpend', () => ({
  getMyBudget: vi.fn(),
  getMyBudgetRuns: vi.fn(),
}));

import { getMyBudget, getMyBudgetRuns } from '@/services/budgetSpend';

const mockGetMyBudget = getMyBudget as ReturnType<typeof vi.fn>;
const mockGetMyBudgetRuns = getMyBudgetRuns as ReturnType<typeof vi.fn>;

// Nav gating inputs. The defaults describe a MEMBER: no admin role and none of the
// admin view permissions — which is the persona the screen exists for.
const mockUsePermissions = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockUsePermissions(),
}));

const mockUseFeatures = vi.fn();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
}));

vi.mock('@/services/auth', () => ({
  getAccessToken: () => null,
}));

/** A MEMBER: authenticated, no admin roles, no admin view permissions. */
function memberPermissions(overrides: Record<string, unknown> = {}) {
  return {
    isPlatformAdmin: () => false,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    user: { orgId: 'org-1', deptId: 'dept-1' },
    canViewOrganizations: () => false,
    canViewLogs: () => false,
    canViewMetrics: () => false,
    canViewPool: () => false,
    canViewBudgets: () => false,
    canViewRateLimits: () => false,
    ...overrides,
  };
}

function features(overrides: Partial<FeatureFlags> = {}): FeatureFlags {
  return {
    chat: true,
    knowledge: true,
    indexing: true,
    connections: true,
    credentials: true,
    system_dashboard: true,
    logs: true,
    gitlab: false,
    orchestration_engine: false,
    budget_spend: true,
    ...overrides,
  };
}

function createTestQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

function renderScreen() {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter>
        <BudgetSpend />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('BudgetSpend — a member sees their own figures', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('renders the nav item for a member-role token', async () => {
    // Criterion 1. The nav entry is ungated by permission: server-side scoping is the
    // control. Gating on a permission a MEMBER lacks would ship the screen invisible to
    // exactly the people it was built for (#4389).
    render(
      <MemoryRouter>
        <Navigation />
      </MemoryRouter>,
    );

    const link = screen.getByRole('link', { name: /Budget & Spend/i });
    expect(link).toHaveAttribute('href', '/budget');
  });

  it('renders the binding line as the headline with cap, spend and headroom', async () => {
    renderScreen();

    // The headline is the BINDING line (lowest-remaining capped), never a sum of lines.
    // Scoped to the headline: the binding line's label and figures legitimately appear
    // again in the per-line list below, so an unscoped query matches twice.
    await waitFor(() => expect(screen.getByTestId('headline-binding')).toBeInTheDocument());
    const headline = screen.getByTestId('headline-binding');
    expect(within(headline).getByRole('heading', { name: 'Cloud agent runs' })).toBeInTheDocument();
    expect(within(headline).getByText('$200.00')).toBeInTheDocument();
    expect(within(headline).getByText('$28.60')).toBeInTheDocument();

    // And it is NOT the sum of the two lines' caps ($800) or spends ($584.20).
    expect(screen.queryByText('$800.00')).not.toBeInTheDocument();
    expect(headline.textContent).not.toContain('$584.20');
  });

  it('renders an uncapped caller without inventing a cap', async () => {
    mockGetMyBudget.mockResolvedValue({
      ...mockBudgetEnvelope,
      cap_usd: null,
      remaining_usd: null,
      utilization_pct: null,
      band: null,
      cap_status: 'uncapped',
      enforcement_mode: null,
      binding: null,
      lines: [mockUncappedLine],
      combined_informational: null,
    });
    renderScreen();

    // `binding: null` means nothing is capped. Not an error, and not a $0 cap.
    await waitFor(() => expect(screen.getByTestId('headline-uncapped')).toBeInTheDocument());
    expect(screen.queryByTestId('headline-binding')).not.toBeInTheDocument();
  });

  it('reports a backend failure as a failure, never as zero spend', async () => {
    mockGetMyBudget.mockRejectedValue(new Error('503'));
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('budget-error')).toBeInTheDocument());
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });
});

describe('BudgetSpend — period selector', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('offers exactly three options, and no RUN or CHAIN option', async () => {
    // Criterion 2. run/chain caps are lifetime-scoped, have no calendar window, and the
    // endpoint rejects them with a 422 — offering them offers a query that cannot succeed.
    renderScreen();

    const group = screen.getByRole('group', { name: /Budget period/i });
    const options = within(group).getAllByRole('button');
    expect(options).toHaveLength(3);
    expect(options.map((o) => o.textContent)).toEqual(['Daily', 'Weekly', 'Monthly']);

    expect(screen.queryByText(/^run$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^chain$/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('period-option-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('period-option-chain')).not.toBeInTheDocument();
  });

  it('refetches for the chosen period', async () => {
    renderScreen();
    await waitFor(() => expect(mockGetMyBudget).toHaveBeenCalledWith('monthly'));

    await userEvent.click(screen.getByTestId('period-option-daily'));
    await waitFor(() => expect(mockGetMyBudget).toHaveBeenCalledWith('daily'));
  });
});

describe('BudgetSpend — shadow mode copy', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('renders the shadow-mode banner while enforcement_mode is shadow', async () => {
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('shadow-mode-banner')).toBeInTheDocument());
  });

  it('never claims spend will be stopped anywhere on the screen', async () => {
    // Criterion 6. Caps are advisory in shadow mode, so this copy would be false — and
    // a screen that threatens a consequence it cannot deliver erodes trust in the rest
    // of the figures. Asserted over the whole document, not just the banner.
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    const { container } = renderScreen();

    await waitFor(() => expect(screen.getByTestId('shadow-mode-banner')).toBeInTheDocument());

    const text = container.textContent ?? '';
    expect(text).not.toMatch(/will be stopped/i);
    expect(text).not.toMatch(/will be blocked/i);
    expect(text).not.toMatch(/requests? (are|will be) (blocked|denied|halted)/i);
    // The positive statement the banner must make instead.
    expect(text).toMatch(/advisory/i);
  });

  it('omits the banner when the binding cap is hard-enforced', async () => {
    mockGetMyBudget.mockResolvedValue({
      ...mockBudgetEnvelope,
      enforcement_mode: 'hard',
      binding: { ...mockBudgetEnvelope.binding!, enforcement_mode: 'hard' },
    });
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('headline-binding')).toBeInTheDocument());
    expect(screen.queryByTestId('shadow-mode-banner')).not.toBeInTheDocument();
  });
});

describe('BudgetSpend — freshness affordance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('renders the freshness notice when back-fill lag is signalled', async () => {
    // Criterion 8. Cost settles asynchronously, so the figures are a lower bound and
    // saying so beats implying real-time truth.
    mockGetMyBudget.mockResolvedValue({ ...mockBudgetEnvelope, freshness: { cost_backfill_lag: true } });
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('freshness-notice')).toBeInTheDocument());
    expect(screen.getByTestId('freshness-notice').textContent).toMatch(/lower bound|may be higher/i);
  });

  it('omits the notice when every recent request has settled', async () => {
    mockGetMyBudget.mockResolvedValue({ ...mockBudgetEnvelope, freshness: { cost_backfill_lag: false } });
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('headline-binding')).toBeInTheDocument());
    expect(screen.queryByTestId('freshness-notice')).not.toBeInTheDocument();
  });

  it('reads freshness as an object, so a flattened boolean cannot silently disable it', async () => {
    // The #3675 shape guard: `freshness` is an OBJECT on the wire. If it were read as a
    // bare boolean, the affordance would never render and mock-backed tests would still
    // pass. This asserts the object path is the one being read.
    expect(mockBudgetEnvelope.freshness).toEqual({ cost_backfill_lag: false });
    mockGetMyBudget.mockResolvedValue({ ...mockBudgetEnvelope, freshness: { cost_backfill_lag: true } });
    renderScreen();
    await waitFor(() => expect(screen.getByTestId('freshness-notice')).toBeInTheDocument());
  });
});

describe('BudgetSpend — run drill-down cost rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('renders an unknown cost as an em dash, never as $0.00', async () => {
    // Criterion 5. A run with no usage row yet demonstrably did work; $0.00 would say
    // it was free. The fixture's `unknown` run carries no amount at all.
    renderScreen();

    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(3));

    const unknownCell = screen.getByText('—', { selector: '[data-cost-status="unknown"]' });
    expect(unknownCell).toBeInTheDocument();
  });

  it('renders an unknown cost as a dash even when the wire wrongly attaches a zero', async () => {
    // The status is authoritative over the number. This is the exact seam where an
    // `unknown` carrying 0 would become $0.00 three layers away.
    mockGetMyBudgetRuns.mockResolvedValue({
      ...mockBudgetRuns,
      items: [{ ...mockBudgetRuns.items[1], cost: { status: 'unknown', amount_usd: '0', reason: 'no_usage_rows', partial: false } }],
      subtotal: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows', partial: false },
    });
    renderScreen();

    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(1));
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('renders a verified zero as $0.00, which is honest', async () => {
    // `none_incurred` is a MEASURED zero — rows exist and total zero — and must not be
    // hidden behind a dash. The opposite failure to the one above.
    renderScreen();

    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(3));
    expect(screen.getByText('$0.00', { selector: '[data-cost-status="none_incurred"]' })).toBeInTheDocument();
  });

  it('labels the subtotal as covering this page, not the period', async () => {
    // A page figure captioned as a period total is the class of wrong number the EPIC
    // exists to eliminate.
    renderScreen();

    await waitFor(() => expect(screen.getByText(/Subtotal for these 3 runs/i)).toBeInTheDocument());
  });

  it('marks a partial subtotal as a lower bound', async () => {
    renderScreen();

    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(3));
    expect(screen.getByText(/or more — partial total/i)).toBeInTheDocument();
  });
});

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

describe('BudgetSpend — no misleading tile affordances', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('passes no change= prop to any spend tile', async () => {
    // Criterion 9. StatCard's `change` hardcodes "from yesterday" and colours increases
    // GREEN — for spend, an increase is not good news. Asserted through the rendered
    // output the prop would produce, so it cannot be reintroduced unnoticed.
    const { container } = renderScreen();

    await waitFor(() => expect(screen.getByTestId('headline-binding')).toBeInTheDocument());
    // "from yesterday" is the literal string `change` renders, and it must appear
    // nowhere on the screen.
    expect(container.textContent).not.toContain('from yesterday');
    // The green-increase treatment is checked inside the TILES specifically. A
    // green elsewhere is fine and expected — a run whose status is `complete` is
    // legitimately green — but a green delta on a spend tile would be saying an
    // increase in spend is good news.
    expect(screen.getByTestId('headline-binding').querySelector('.text-green-600')).toBeNull();
    // The arrow glyphs `change` renders, likewise absent from the tiles.
    expect(screen.getByTestId('headline-binding').textContent).not.toMatch(/[↑↓]/);
  });
});

describe('BudgetSpend — feature flag', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('hides the nav item when the flag is off', () => {
    // Criterion 7, half one. The documented rollback is "flip the flag off — screen and
    // nav vanish, no redeploy", so both halves must actually vanish.
    mockUseFeatures.mockReturnValue(features({ budget_spend: false }));
    render(
      <MemoryRouter>
        <Navigation />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: /Budget & Spend/i })).not.toBeInTheDocument();
  });

  it('redirects away from the route when the flag is off', () => {
    // Criterion 7, half two: the route must not be reachable by typing the URL either.
    mockUseFeatures.mockReturnValue(features({ budget_spend: false }));
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={['/budget']}>
          <Routes>
            <Route path="/" element={<div>home</div>} />
            <Route
              path="/budget"
              element={
                <FeatureGate feature="budget_spend">
                  <BudgetSpend />
                </FeatureGate>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByText('home')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Budget & Spend/i })).not.toBeInTheDocument();
  });

  it('renders the screen when the flag is on', async () => {
    mockUseFeatures.mockReturnValue(features({ budget_spend: true }));
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={['/budget']}>
          <Routes>
            <Route path="/" element={<div>home</div>} />
            <Route
              path="/budget"
              element={
                <FeatureGate feature="budget_spend">
                  <BudgetSpend />
                </FeatureGate>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('headline-binding')).toBeInTheDocument());
  });
});

describe('BudgetSpend — absent cloud ledger', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
  });

  it('says cloud spend is missing rather than zero when identity is unresolved', async () => {
    // `unresolved` means the cloud ledger could not be looked up, so it is ABSENT from
    // the figures. Reading that as "no cloud spend" is the EPIC's headline failure: a
    // screen saying "you have spent nothing" when the truth is "we could not look".
    mockGetMyBudget.mockResolvedValue({ ...mockBudgetEnvelope, identity_status: 'unresolved' });
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('identity-unresolved')).toBeInTheDocument());
    expect(screen.getByTestId('identity-unresolved').textContent).toMatch(/not a statement that it is zero/i);
  });
});
