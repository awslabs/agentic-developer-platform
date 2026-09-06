/**
 * Tests for the ONE spend element — Issue #4685 (final #4669 ruling, 2026-09-05).
 *
 * The ruling this file guards is a *count*: `/budget` shows **exactly one** headline
 * spend figure — "you can spend $X; you've spent $Y" — tracked against the same number
 * enforcement uses. The page previously showed five figures, then briefly two; the
 * operator could not tell which one governed them. Counting is the only assertion that
 * holds the line.
 *
 * The other gates, each a review finding on #4686 or a defect that already shipped:
 *
 *  - **The card mounts without the envelope.** The personal limit is its own endpoint;
 *    an outage on /me/budget must not hide the control that can unblock a person whose
 *    hard limit is stopping runs.
 *  - **Readability is the parse result, never `== null`.** An empty-string figure gets
 *    the "could not be read" caveat and NO bar — never a dash beside a confident 0%.
 *  - **A bar only when something enforces**, clamped for ARIA with the TRUE overage in
 *    `aria-valuetext` (AT clamps out-of-range `aria-valuenow` silently).
 *  - **Drill-downs never open onto nothing**, and the runs query is lazy.
 *  - **The numerator is the envelope figure alone** — never summed with `direct`
 *    (the #4322 double-count family, on the headline).
 *
 * Fixtures come from `mocks/data/budgetSpend.ts`, transcribed from
 * `src/budget/schemas.py` — never from `src/types/budget.ts` (the #3675 guard).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MySpend } from '@/components/budget/SpendTiles';
import {
  mockBudgetEnvelope,
  mockBudgetRuns,
  mockPersonCap,
  mockPersonCapEnforcing,
  mockPersonCapUncapped,
} from '@/mocks/data/budgetSpend';
import type { BudgetEnvelopeResponse } from '@/types/budget';

vi.mock('@/services/personCap', () => ({
  getMyPersonCap: vi.fn(),
  setMyPersonCap: vi.fn(),
  deleteMyPersonCap: vi.fn(),
}));
vi.mock('@/services/budgetSpend', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/budgetSpend')>()),
  getMyBudget: vi.fn(),
  getMyBudgetRuns: vi.fn(),
}));

import { getMyPersonCap } from '@/services/personCap';
import { getMyBudgetRuns } from '@/services/budgetSpend';

const mockGetCap = getMyPersonCap as ReturnType<typeof vi.fn>;
const mockGetRuns = getMyBudgetRuns as ReturnType<typeof vi.fn>;

function createTestQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
}

// No default parameter: renderCard(undefined) must genuinely mean "no envelope" —
// a fixture default would silently swallow the outage-independence case.
function renderCard(envelope: BudgetEnvelopeResponse | undefined) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MySpend envelope={envelope} period="monthly" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetCap.mockResolvedValue(mockPersonCapEnforcing);
  mockGetRuns.mockResolvedValue(mockBudgetRuns);
});

describe('MySpend — the one headline figure', () => {
  it('renders exactly one headline spend figure, from the envelope alone', async () => {
    renderCard(mockBudgetEnvelope);

    await waitFor(() => expect(screen.getByTestId('my-spend-amount')).toBeInTheDocument());
    expect(document.querySelectorAll('[data-testid="my-spend-amount"]')).toHaveLength(1);
    // The envelope figure, never a client-side sum with `direct` (#4322 family).
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('$415.05');
    expect(document.body.textContent).not.toContain('$584.20');
  });

  it('mounts the card and the limit editor WITHOUT an envelope (outage independence)', async () => {
    renderCard(undefined);

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
    // The figure degrades honestly…
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('—');
    expect(screen.getByTestId('my-spend-unreported')).toBeInTheDocument();
    // …while the limit editor — a separate, healthy endpoint — still mounts: it is
    // the control that can unblock a person whose hard limit is stopping runs.
    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    // No drill-downs onto data that never arrived.
    expect(screen.queryByTestId('my-spend-drilldown-orgs')).not.toBeInTheDocument();
    expect(screen.queryByTestId('my-spend-drilldown-runs')).not.toBeInTheDocument();
  });

  it('treats an empty-string figure as unreadable: caveat shown, NO bar drawn', async () => {
    // `Number('') === 0`; gating on `== null` would render "we could not read your
    // spend" and a confident 0% bar on the same card (review fix on #4686).
    renderCard({
      ...mockBudgetEnvelope,
      person_envelope: { ...mockBudgetEnvelope.person_envelope!, spend_usd: '' },
    });

    await waitFor(() => expect(screen.getByTestId('my-spend-unreported')).toBeInTheDocument());
    expect(screen.getByTestId('my-spend-amount')).toHaveTextContent('—');
    await waitFor(() => expect(screen.getByTestId('my-spend-limit')).toBeInTheDocument());
    expect(screen.queryByTestId('my-spend-bar')).not.toBeInTheDocument();
  });
});

describe('MySpend — the limit denominator', () => {
  it('draws a bar for a hard limit, clamped for ARIA with the true overage in valuetext', async () => {
    // spend $415.05 against the $250 enforcing fixture = 166.0%.
    renderCard(mockBudgetEnvelope);

    const bar = await screen.findByTestId('my-spend-bar');
    // ARIA numeric contract: valuenow stays in [min,max] because AT clamps silently…
    expect(bar).toHaveAttribute('aria-valuenow', '100');
    // …and the TRUE figure rides valuetext, so a screen-reader hears the overage.
    expect(bar).toHaveAttribute('aria-valuetext', '166.0% of your personal limit used');
    expect(screen.getByTestId('my-spend-caption')).toHaveTextContent(/enforcing/);
  });

  it('renders a soft (pre-C4) limit with the figure, NO bar, and "not enforced yet"', async () => {
    mockGetCap.mockResolvedValue(mockPersonCap);
    renderCard(mockBudgetEnvelope);

    await waitFor(() => expect(screen.getByTestId('my-spend-limit')).toBeInTheDocument());
    expect(screen.queryByTestId('my-spend-bar')).not.toBeInTheDocument();
    expect(screen.getByTestId('my-spend-caption')).toHaveTextContent(/not enforced yet/);
  });

  it('renders no denominator at all for an uncapped caller', async () => {
    mockGetCap.mockResolvedValue(mockPersonCapUncapped);
    renderCard(mockBudgetEnvelope);

    await waitFor(() => expect(screen.getByTestId('my-spend-amount')).toBeInTheDocument());
    expect(screen.queryByTestId('my-spend-limit')).not.toBeInTheDocument();
    expect(screen.queryByTestId('my-spend-bar')).not.toBeInTheDocument();
  });
});

describe('MySpend — drill-downs', () => {
  it('keeps the direct line and other capped lines reachable, one click away', async () => {
    renderCard(mockBudgetEnvelope);

    const lines = await screen.findByTestId('my-spend-drilldown-lines');
    await userEvent.click(within(lines).getByText(/direct use & other lines/i));

    const rows = screen.getAllByTestId('budget-line-row');
    const direct = rows.find((row) => row.getAttribute('data-source') === 'direct');
    expect(direct).toBeDefined();
    expect(direct!.textContent).toContain('$412.80');
  });

  it('states an ancestor binding cap inside the lines drill-down, not nowhere', async () => {
    // A team/dept/org cap can bind without appearing in `lines`; deleting the old
    // binding headline must not delete the figure that will actually stop the
    // caller (review fix on #4686).
    renderCard({
      ...mockBudgetEnvelope,
      binding: {
        ...mockBudgetEnvelope.lines[0],
        entity_type: 'organization',
        label: 'Organization budget',
        remaining_usd: '12.000000',
      },
    });

    const lines = await screen.findByTestId('my-spend-drilldown-lines');
    await userEvent.click(within(lines).getByText(/direct use & other lines/i));

    const note = screen.getByTestId('binding-ancestor-note');
    expect(note.textContent).toContain('Organization budget');
    expect(note.textContent).toContain('$12.00');
  });

  it('omits the ancestor note when the binding line is one of the visible lines', async () => {
    renderCard(mockBudgetEnvelope); // fixture binding mirrors a line already in `lines`

    const lines = await screen.findByTestId('my-spend-drilldown-lines');
    await userEvent.click(within(lines).getByText(/direct use & other lines/i));
    expect(screen.queryByTestId('binding-ancestor-note')).not.toBeInTheDocument();
  });

  it('never mounts the by-GitHub-org drill-down onto nothing', async () => {
    renderCard({ ...mockBudgetEnvelope, identity_status: 'unresolved', per_org: [], person_envelope: null });

    await waitFor(() => expect(screen.getByTestId('my-spend')).toBeInTheDocument());
    expect(screen.queryByTestId('my-spend-drilldown-orgs')).not.toBeInTheDocument();
  });

  it('fetches the runs list lazily, on first expand only', async () => {
    renderCard(mockBudgetEnvelope);

    await screen.findByTestId('my-spend-drilldown-runs');
    // Collapsed: a table nobody expanded is a query nobody needed.
    expect(mockGetRuns).not.toHaveBeenCalled();

    await userEvent.click(within(screen.getByTestId('my-spend-drilldown-runs')).getByText(/agent runs/i));
    await waitFor(() => expect(mockGetRuns).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getAllByTestId('budget-run-row').length).toBeGreaterThan(0));
    expect(screen.getByTestId('runs-scope-note')).toBeInTheDocument();
  });
});
