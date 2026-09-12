/**
 * Tests for the Budget & Spend screen — Issue #4402 (U-5 of EPIC #4324), retargeted to the
 * two-tile shape by #4685.
 *
 * Covers the issue's numbered validation criteria: a member sees the nav item and their
 * own figures (1), exactly three period options with no RUN/CHAIN (2), `unknown` cost
 * rendering `—` rather than `$0.00` (5), the shadow-mode banner with no
 * "will be stopped" copy (6), flag off removing both route and nav (7), the freshness
 * affordance (8), and no `change=` prop on any spend tile (9).
 *
 * **What #4685 changed here.** The page's spend surface is now two tiles rendered by
 * `SpendTiles`, so assertions that reached for `headline-binding`, `combined-informational`
 * or `person-envelope` now reach for the single `my-spend` card. Three tests
 * were deleted rather than retargeted, because the elements they guarded no longer exist:
 * the person-envelope total's verbatim server note, its no-progressbar constraint, and its
 * "never called a budget" copy. That coverage did not evaporate — `SpendTiles.test.tsx`
 * asserts the same rules against the Cloud tile that replaced it, including that a bar
 * appears only when a `hard` limit actually enforces the denominator.
 *
 * The page-level tests that remain are the ones genuinely about the *page*: period
 * selection, the three qualifying notices, the error path, the feature flag, and the fact
 * that the figures the tiles show are the caller's own.
 *
 * Fixtures come from `mocks/data/budgetSpend.ts`, transcribed from
 * `src/budget/schemas.py`. Writing them from the frontend type is what let #3675 ship a
 * dashboard whose mocks, tests and eval all validated fields the backend never sent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import BudgetSpend from '@/pages/BudgetSpend';
import { BudgetRunsTable } from '@/components/budget/BudgetRunsTable';
import { Navigation } from '@/components/Navigation';
import { FeatureGate } from '@/components/FeatureGate';
import { mockBudgetEnvelope, mockBudgetRuns, mockUncappedLine, mockPerOrgLines, mockPersonCapEnforcing, mockPersonEnvelope } from '@/mocks/data/budgetSpend';
import type { FeatureFlags } from '@/services/features';
import type { BudgetEnvelopeResponse } from '@/types/budget';

vi.mock('@/services/budgetSpend', () => ({
  getMyBudget: vi.fn(),
  getMyBudgetRuns: vi.fn(),
}));

// The personal-limit read moved inside the Cloud tile (#4685), so the page now pulls it
// too. Mocked here for the same reason the budget service is: an unmocked module would
// hit the real axios client and every test would depend on network behaviour.
vi.mock('@/services/personCap', () => ({
  getMyPersonCap: vi.fn(),
}));

import { getMyBudget, getMyBudgetRuns } from '@/services/budgetSpend';
import { getMyPersonCap } from '@/services/personCap';

const mockGetMyBudget = getMyBudget as ReturnType<typeof vi.fn>;
const mockGetMyBudgetRuns = getMyBudgetRuns as ReturnType<typeof vi.fn>;
const mockGetMyPersonCap = getMyPersonCap as ReturnType<typeof vi.fn>;

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
    // Issue #3960. Declared `false` because this returns a full FeatureFlags: a
    // missing key would be `undefined`, which reads as off for a gate but is not
    // the same as declaring it off — and a later flag flip here would be silent.
    agent_control: false,
    ...overrides,
  };
}

/** The runs list is lazy since the one-card reshape: expand its drill-down first. */
async function openRunsDrilldown() {
  await waitFor(() => expect(screen.getByTestId('my-spend-drilldown-runs')).toBeInTheDocument());
  await userEvent.click(within(screen.getByTestId('my-spend-drilldown-runs')).getByText(/agent runs/i));
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
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
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

  it('presents exactly ONE spend element, and no summed figure (final #4669 ruling)', async () => {
    // The page-level half of the #4669 ruling: whatever the tiles do internally, the
    // PAGE must put exactly one spend element in front of the reader. It used to mount
    // five, and the operator who designed the budget model could not tell which of them
    // governed him.
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
    // ONE headline figure — the final #4669 ruling. The count is the contract.
    expect(document.querySelectorAll('[data-testid="my-spend-amount"]')).toHaveLength(1);
    expect(document.querySelectorAll('[data-testid$="spend-tile"]')).toHaveLength(0);

    // The page-level double-count still must not appear: $584.20 is this partition's
    // direct+cloud added together by a client, against a cap that governs neither half.
    // ($827.85 is NOT in this class since #4396 — it is the server's own fused person
    // total, computed over two disjoint ledgers and enforced as one figure, so it is the
    // headline rather than a forbidden sum.)
    const rendered = document.body.textContent ?? '';
    expect(rendered).not.toContain('$584.20');
  });

  it('shows the caller their own direct and cloud figures', async () => {
    // Criterion 1, in the two-tile shape: the direct line's own spend, and the
    // cross-workspace person envelope. Each measured against the cap that governs it.
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('my-spend-amount')).toBeInTheDocument());
    // The headline is the enforced figure — the fused person total (#4396) — and only that.
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('$827.85');
    // The direct figure is a drill-down, not a sibling headline (final ruling).
    await userEvent.click(within(screen.getByTestId('my-spend-drilldown-lines')).getByText(/direct use & other lines/i));
    const directRow = screen.getAllByTestId('budget-line-row').find((row) => row.getAttribute('data-source') === 'direct');
    expect(directRow).toBeDefined();
    expect(directRow!.textContent).toContain('$412.80');
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
      // No inherited per-org card: its active row carries "Cap here $200.00",
      // which contradicts this test's uncapped premise (review fix).
      per_org: [],
      person_envelope: null,
      lines: [mockUncappedLine],
      combined_informational: null,
    });
    renderScreen();

    // Nothing is capped, so the tile says so in words. Not an error, and not a $0 cap —
    // "no cap configured" and "a ceiling of zero dollars" are opposite claims.
    await waitFor(() => expect(screen.getByTestId('my-spend-drilldown-lines')).toBeInTheDocument());
    await userEvent.click(within(screen.getByTestId('my-spend-drilldown-lines')).getByText(/direct use & other lines/i));
    const directRow = screen.getAllByTestId('budget-line-row').find((row) => row.getAttribute('data-source') === 'direct');
    expect(directRow!.textContent).toContain('$31.25');
    expect(directRow!.textContent).not.toContain('$0.00');
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
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
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
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
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

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
    expect(screen.queryByTestId('shadow-mode-banner')).not.toBeInTheDocument();
  });
});

describe('BudgetSpend — freshness affordance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
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

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
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
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
  });

  it('renders an unknown cost as an em dash, never as $0.00', async () => {
    // Criterion 5. A run with no usage row yet demonstrably did work; $0.00 would say
    // it was free. The fixture's `unknown` run carries no amount at all.
    renderScreen();

    await openRunsDrilldown();
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

    await openRunsDrilldown();
    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(1));
    // Scoped to the runs drill-down, not the document: a `<details>` keeps its content in
    // the DOM while collapsed, so an unscoped query also sees the per-workspace rows —
    // where a `'0.000000'` direct figure is a MEASURED zero and `$0.00` is the honest
    // rendering. This assertion is about the `unknown` cost cell only.
    expect(within(screen.getByTestId('my-spend-drilldown-runs')).queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('renders a verified zero as $0.00, which is honest', async () => {
    // `none_incurred` is a MEASURED zero — rows exist and total zero — and must not be
    // hidden behind a dash. The opposite failure to the one above.
    renderScreen();

    await openRunsDrilldown();
    await waitFor(() => expect(screen.getAllByTestId('budget-run-row')).toHaveLength(3));
    expect(screen.getByText('$0.00', { selector: '[data-cost-status="none_incurred"]' })).toBeInTheDocument();
  });

  it('labels the subtotal as covering this page, not the period', async () => {
    // A page figure captioned as a period total is the class of wrong number the EPIC
    // exists to eliminate.
    renderScreen();
    await openRunsDrilldown();

    await waitFor(() => expect(screen.getByText(/Subtotal for these 3 runs/i)).toBeInTheDocument());
  });

  it('marks a partial subtotal as a lower bound', async () => {
    renderScreen();

    await openRunsDrilldown();
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
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
  });

  it('passes no change= prop to any spend tile', async () => {
    // Criterion 9. StatCard's `change` hardcodes "from yesterday" and colours increases
    // GREEN — for spend, an increase is not good news. Asserted through the rendered
    // output the prop would produce, so it cannot be reintroduced unnoticed.
    const { container } = renderScreen();

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
    // "from yesterday" is the literal string `change` renders, and it must appear
    // nowhere on the screen.
    expect(container.textContent).not.toContain('from yesterday');
    // The green-increase treatment is checked inside the TILES specifically. A
    // green elsewhere is fine and expected — a run whose status is `complete` is
    // legitimately green, and a within-budget band badge is legitimately green — but a
    // green DELTA on a spend figure would be saying an increase in spend is good news.
    for (const testId of ['my-spend-amount']) {
      const figure = screen.getByTestId(testId);
      expect(figure.querySelector('.text-green-600')).toBeNull();
      // The arrow glyphs `change` renders, likewise absent from the figures.
      expect(figure.textContent).not.toMatch(/[↑↓]/);
    }
  });
});

describe('BudgetSpend — feature flag', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
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

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
  });
});

describe('BudgetSpend — absent cloud ledger', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
  });

  it('says cloud spend is missing rather than zero when identity is unresolved', async () => {
    // `unresolved` means the cloud ledger could not be looked up, so it is ABSENT from
    // the figures. Reading that as "no cloud spend" is the EPIC's headline failure: a
    // screen saying "you have spent nothing" when the truth is "we could not look".
    mockGetMyBudget.mockResolvedValue({
      ...mockBudgetEnvelope,
      identity_status: 'unresolved',
      // The wire shape the backend guarantees for this state (review fix: the
      // fixture spread was silently inheriting per_org and rendering exact cloud
      // figures under the "could not be looked up" notice — a response no
      // backend sends).
      per_org: [],
      person_envelope: null,
    });
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('identity-unresolved')).toBeInTheDocument());
    expect(screen.getByTestId('identity-unresolved').textContent).toMatch(/not a statement that it is zero/i);
    // The notice sits ABOVE the tiles, because what it qualifies is the Cloud figure
    // inside one of them — a caveat printed below the number it caveats is read second.
    const notice = screen.getByTestId('identity-unresolved');
    const tiles = screen.getByTestId('my-spend');
    expect(notice.compareDocumentPosition(tiles) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // And the figure itself makes no claim: an em dash, never $0.00.
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('—');
    expect(screen.getByTestId('my-spend-amount').textContent).not.toContain('$0.00');
  });
});

/**
 * Cross-workspace cloud spend — Issue #4646 (C1-UI of #4620), demoted to a drill-down by
 * #4685.
 *
 * The per-workspace breakdown is no longer a top-level card: it is the "by workspace"
 * drill-down inside the Cloud tile, collapsed by default. These tests expand it and assert
 * the rows still carry what #4646 put there — the foreign-partition spend that the
 * single-partition figures omit, the server's active-partition flag, and each workspace's
 * own cap.
 *
 * The three tests that guarded `PersonEnvelopeTotal` are gone with the component. The
 * cross-workspace figure is now the Cloud tile's numerator, and the rules that used to
 * apply to that block — no bar without a cap, never captioned as a budget — are asserted
 * against the tile in `SpendTiles.test.tsx`, where the figure now lives.
 */
describe('BudgetSpend — cloud spend by workspace (drill-down)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUsePermissions.mockReturnValue(memberPermissions());
    mockUseFeatures.mockReturnValue(features());
    mockGetMyBudget.mockResolvedValue(mockBudgetEnvelope);
    mockGetMyBudgetRuns.mockResolvedValue(mockBudgetRuns);
    mockGetMyPersonCap.mockResolvedValue(mockPersonCapEnforcing);
  });

  /** Expand the collapsed "by workspace" drill-down and wait for its rows. */
  async function openWorkspaceDrillDown() {
    await waitFor(() => expect(screen.getByTestId('my-spend-drilldown-orgs')).toBeInTheDocument());
    await userEvent.click(within(screen.getByTestId('my-spend-drilldown-orgs')).getByText(/by GitHub org/i));
  }

  it('is collapsed until the reader asks for it', async () => {
    // The ruling's demotion: this used to be a card competing with the headline figures.
    renderScreen();

    await waitFor(() => expect(screen.getByTestId('my-spend-drilldown-orgs')).toBeInTheDocument());
    expect(screen.getByTestId('my-spend-drilldown-orgs')).not.toHaveAttribute('open');
  });

  it('renders one row per per_org line, with each workspace named', async () => {
    renderScreen();
    await openWorkspaceDrillDown();

    const rows = screen.getAllByTestId('per-org-row');
    expect(rows).toHaveLength(2);
    // Order is the server's: active partition FIRST, so these rows agree with the
    // figures in the tiles above them.
    expect(rows.map((r) => r.getAttribute('data-org-id'))).toEqual(['org-1', 'org-aws-e']);
    expect(within(rows[0]).getByText('Pranav Sharma (home)')).toBeInTheDocument();
    expect(within(rows[1]).getByText('aws-e')).toBeInTheDocument();
  });

  it('shows the foreign partition spend that the single-partition figures omit', async () => {
    // The whole point of #4620: $243.65 accrued in a tenant the session is not attributed
    // to, so it appears in NEITHER of the single-partition figures. If this row is
    // missing, the operator still reads their cross-workspace spend as absent.
    renderScreen();
    await openWorkspaceDrillDown();

    const foreign = screen.getAllByTestId('per-org-row')[1];
    expect(within(foreign).getByTestId('per-org-spend').textContent).toBe('$243.65');
    // Genuinely absent from Personal spend, which is this workspace's direct use only.
    // The foreign-org figure appears only inside its drill-down row, never in the headline copy above it.
    expect(screen.getByTestId('my-spend-amount').textContent).not.toContain('243.65');
    // But included in the headline numerator, which is the cross-workspace total: that is
    // the relationship between the card and its own drill-down.
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('$827.85');
  });

  it('shows each workspace\'s agent and direct spend side by side, never added (#4396)', async () => {
    // The personal limit now governs both halves, so the breakdown has to show both —
    // otherwise a reader whose limit is being consumed by interactive use sees rows that
    // account for only part of the number above them. Side by side and NOT summed: the
    // total has exactly one home, the headline.
    renderScreen();
    await openWorkspaceDrillDown();

    const active = screen.getAllByTestId('per-org-row')[0];
    expect(within(active).getByTestId('per-org-spend').textContent).toBe('$171.40');
    expect(within(active).getByTestId('per-org-direct-spend').textContent).toBe('$412.80');
    // No per-row total: $584.20 is 171.40 + 412.80, a figure no cap governs.
    expect(active.textContent).not.toContain('$584.20');
    // The labels say which ledger each cell is, so the two are not read as one.
    expect(within(active).getByText('Agents')).toBeInTheDocument();
    expect(within(active).getByText('Direct')).toBeInTheDocument();
  });

  it('renders a measured zero of direct spend as $0.00, and an absent one as a dash', async () => {
    // Two opposite failures on one cell. A person's interactive spend lands in whichever
    // tenant they were signed into, so `'0.000000'` in a foreign partition is a real
    // measurement and `$0.00` is honest. But a response predating #4396 — or a stale cache
    // mid-rollout — omits the field entirely, and rendering THAT as `$0.00` would claim the
    // person never worked interactively there.
    renderScreen();
    await openWorkspaceDrillDown();
    const foreign = screen.getAllByTestId('per-org-row')[1];
    expect(within(foreign).getByTestId('per-org-direct-spend').textContent).toBe('$0.00');

    // Now the pre-#4396 shape: the key is genuinely ABSENT, not undefined.
    const legacyLine: Record<string, unknown> = { ...mockPerOrgLines[0] };
    delete legacyLine.direct_spend_usd;
    expect('direct_spend_usd' in legacyLine).toBe(false);
    mockGetMyBudget.mockResolvedValue({ ...mockBudgetEnvelope, per_org: [legacyLine as unknown as (typeof mockPerOrgLines)[0]] });
    cleanup();
    renderScreen();
    await openWorkspaceDrillDown();

    const legacyRow = screen.getAllByTestId('per-org-row')[0];
    expect(within(legacyRow).getByTestId('per-org-direct-spend').textContent).toBe('—');
    // The cloud cell beside it is unaffected: one missing field is not a broken row.
    expect(within(legacyRow).getByTestId('per-org-spend').textContent).toBe('$171.40');
  });

  it('flags only the active partition', async () => {
    renderScreen();
    await openWorkspaceDrillDown();

    const rows = screen.getAllByTestId('per-org-row');
    // Read off the server's `is_active_partition`, never re-derived from the token.
    expect(within(rows[0]).getByTestId('per-org-active-badge')).toBeInTheDocument();
    expect(within(rows[1]).queryByTestId('per-org-active-badge')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('per-org-active-badge')).toHaveLength(1);
  });

  it("renders a workspace's own cap, and defers to the personal limit where none was authored", async () => {
    // Each tenant's cap governs only spend executing inside it, so caps are per row and
    // never folded together. A null cap is never `$0.00` — but with a personal limit in
    // force it is not "ungoverned" either, so the row says which ceiling actually applies
    // (#4685). The bare "No cap set" case is covered in SpendTiles.test.tsx, where the
    // caller has no personal limit.
    renderScreen();
    await openWorkspaceDrillDown();

    const rows = screen.getAllByTestId('per-org-row');
    expect(within(rows[0]).getByTestId('per-org-cap').textContent).toBe('$200.00');
    expect(within(rows[1]).getByTestId('per-org-cap').textContent).toMatch(/your personal limit applies/i);
    expect(within(rows[1]).getByTestId('per-org-cap').textContent).not.toContain('$0.00');
  });

  it('renders the page unchanged when the response omits both new fields', async () => {
    // An older API response (predating #4640) has neither field. The breakdown must be
    // empty rather than asserting the caller has no cross-workspace spend — the response
    // never spoke to the question — and nothing else on the screen may change or throw.
    const legacy: BudgetEnvelopeResponse = { ...mockBudgetEnvelope };
    delete legacy.per_org;
    delete legacy.person_envelope;
    // Both keys are genuinely ABSENT, not merely undefined — which is what an older
    // backend actually sends, and the case a `?? []` guard has to survive.
    expect('per_org' in legacy).toBe(false);
    expect('person_envelope' in legacy).toBe(false);
    mockGetMyBudget.mockResolvedValue(legacy);
    renderScreen();
    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());

    // Stronger than empty rows: the drill-down itself never mounts onto nothing —
    // a clickable expander opening an empty pane would read as "no cross-org
    // spend" when the response never spoke to the question (review fix on #4686).
    expect(screen.queryByTestId('my-spend-drilldown-orgs')).not.toBeInTheDocument();
    expect(screen.queryByTestId('per-org-row')).not.toBeInTheDocument();
    // The cloud figure makes no claim either, rather than reporting a zero.
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('—');
    // Everything genuinely unrelated still renders.
    await userEvent.click(within(screen.getByTestId('my-spend-drilldown-lines')).getByText(/direct use & other lines/i));
    const direct = screen.getAllByTestId('budget-line-row').find((row) => row.getAttribute('data-source') === 'direct');
    expect(direct!.textContent).toContain('$412.80');
    expect(screen.getByTestId('shadow-mode-banner')).toBeInTheDocument();
  });

  it('draws no fabricated $0 workspace line when identity did not resolve', async () => {
    // `per_org: []` with `person_envelope: null` is what the backend sends when the
    // caller's canonical id could not be resolved. The page already states that the cloud
    // ledger is MISSING rather than zero; a $0 workspace row would contradict that notice
    // with a figure it does not have.
    mockGetMyBudget.mockResolvedValue({ ...mockBudgetEnvelope, identity_status: 'unresolved', per_org: [], person_envelope: null });
    renderScreen();
    await waitFor(() => expect(screen.getByTestId('identity-unresolved')).toBeInTheDocument());

    // The drill-down never mounts for an unresolved identity (review fix on #4686).
    expect(screen.queryByTestId('my-spend-drilldown-orgs')).not.toBeInTheDocument();
    expect(screen.queryByTestId('per-org-row')).not.toBeInTheDocument();
  });

  it('counts a single partition in the cloud figure, which is still a useful claim', async () => {
    // "This is your total everywhere" is distinct and useful even when everywhere is one
    // workspace, so the figure ships rather than being suppressed for a person whose
    // spend has not yet crossed a boundary.
    mockGetMyBudget.mockResolvedValue({
      ...mockBudgetEnvelope,
      per_org: [mockPerOrgLines[0]],
      person_envelope: { ...mockPersonEnvelope, spend_usd: '171.400000', partition_count: 1 },
    });
    renderScreen();
    await openWorkspaceDrillDown();

    expect(screen.getAllByTestId('per-org-row')).toHaveLength(1);
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('$171.40');
  });
});
