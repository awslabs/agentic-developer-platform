/**
 * Tests for the envelope lines — Issue #4402 (U-5), trimmed by #4685.
 *
 * The load-bearing assertions here are the negative ones: a bar is drawn **only** where
 * the wire carries a real cap, because a bar without a denominator has users planning
 * against an invented ceiling. A test that only checked a figure appeared would pass just
 * as happily with a spurious bar beside it, so each absence is asserted explicitly.
 *
 * **The combined-total block is gone (#4685).** `CombinedTotal` rendered the direct+cloud
 * sum, and its tests asserted the right things about it — no progressbar, no denominator,
 * captioned as not-a-budget. The #4669 ruling deleted the component rather than fixing it:
 * a figure no cap governs had no business competing with the two governed figures at the
 * top of `/budget`, however carefully it was captioned. `SpendTiles.test.tsx` now asserts
 * that the sum appears nowhere on the page.
 *
 * `BudgetLines` itself is no longer mounted by `/budget` (the two tiles are that page's
 * whole spend surface) but remains the canonical rendering of a separately-capped line,
 * including the `service:`-principal affordance of criterion 10 — so these tests stay.
 *
 * Fixtures come from `mocks/data/budgetSpend.ts`, transcribed from
 * `src/budget/schemas.py` — never from `src/types/budget.ts` (the #3675 guard).
 */

import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { BudgetLines, BudgetLineRow } from '@/components/budget/BudgetLines';
import { mockDirectLine, mockCloudLine, mockServiceLine, mockUncappedLine } from '@/mocks/data/budgetSpend';
import type { BudgetLine } from '@/types/budget';

/** Build a line at a given utilisation + band, as the SERVER would report it. */
function lineAt(utilization_pct: number, band: BudgetLine['band']): BudgetLine {
  return { ...mockDirectLine, utilization_pct, band };
}

describe('BudgetLines — per-line rows', () => {
  it('renders each line with its own cap, spend, headroom and utilisation', () => {
    render(<BudgetLines lines={[mockDirectLine, mockCloudLine]} />);

    const rows = screen.getAllByTestId('budget-line-row');
    expect(rows).toHaveLength(2);

    // Each line carries its OWN cap — they are not merged or summed.
    expect(within(rows[0]).getByText('$600.00')).toBeInTheDocument();
    expect(within(rows[0]).getByText('$412.80')).toBeInTheDocument();
    expect(within(rows[0]).getByText('$187.20')).toBeInTheDocument();
    expect(within(rows[1]).getByText('$200.00')).toBeInTheDocument();
    expect(within(rows[1]).getByText('$171.40')).toBeInTheDocument();
  });

  it('renders an uncapped line as "No cap set", never as $0.00', () => {
    // "No cap configured" and "a cap of $0" are different claims. Rendering the former
    // as $0.00 shows an unlimited user as exhausted.
    render(<BudgetLineRow line={mockUncappedLine} />);

    expect(screen.getByText('No cap set')).toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
    // No cap means no ratio and no bar to fill.
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('renders a negative headroom as negative rather than clamping to zero', () => {
    // Settled spend can pass a cap. The read surface shows the true position.
    const overspent: BudgetLine = { ...mockDirectLine, spend_usd: '650.000000', remaining_usd: '-50.000000', utilization_pct: 108.3, band: 'exceeded' };
    render(<BudgetLineRow line={overspent} />);

    expect(screen.getByText('-$50.00')).toBeInTheDocument();
    expect(screen.getByText('108.3%')).toBeInTheDocument();
  });

  it('reports true utilisation above 100% on the bar while clamping only the fill', () => {
    const overspent: BudgetLine = { ...mockDirectLine, utilization_pct: 108.3, band: 'exceeded' };
    render(<BudgetLineRow line={overspent} />);

    // aria-valuenow keeps the real figure — clamping it would hide the overage.
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '108.3');
  });

  it('renders a $0 cap as a real cap, not as uncapped', () => {
    // A $0 cap is `capped` with cap_usd '0.00' and is always `exceeded`: nothing
    // costing money can pass it. utilization_pct is null (no ratio is defined).
    const zeroCap: BudgetLine = { ...mockDirectLine, cap_usd: '0.00', spend_usd: '0.000000', remaining_usd: '0.000000', utilization_pct: null, band: 'exceeded' };
    render(<BudgetLineRow line={zeroCap} />);

    expect(screen.queryByText('No cap set')).not.toBeInTheDocument();
    expect(screen.getByTestId('band-badge')).toHaveAttribute('data-band', 'exceeded');
  });
});

describe('BudgetLines — bands come from the server, not a local constant', () => {
  // Criterion 3. The thresholds are the server's (80 warn / 95 critical / >=100
  // exceeded). `BudgetManagement.tsx` hardcodes 50/80; a locally-derived band would
  // tell a user they are fine at 79% while the server had already warned them.
  it.each([
    [79, 'none'],
    [85, 'warning'],
    [97, 'critical'],
    [101, 'exceeded'],
  ])('renders %i%% utilisation with the response band %s', (pct, expectedBand) => {
    render(<BudgetLineRow line={lineAt(pct, expectedBand as BudgetLine['band'])} />);
    expect(screen.getByTestId('band-badge')).toHaveAttribute('data-band', expectedBand);
  });

  it('takes the band from the response even when it disagrees with the percentage', () => {
    // The proof that nothing is recomputed client-side: at 79% a 50/80 local constant
    // would say "warning" and an 80/95 one "none". The response says `critical`, and
    // that is what must render — the server is the only authority on the band.
    render(<BudgetLineRow line={lineAt(79, 'critical')} />);
    expect(screen.getByTestId('band-badge')).toHaveAttribute('data-band', 'critical');
  });

  it('renders an uncapped line with no band rather than a reassuring one', () => {
    // Uncapped must not style as `none`/green: that claims an all-clear never measured.
    render(<BudgetLineRow line={mockUncappedLine} />);
    expect(screen.getByTestId('band-badge')).toHaveAttribute('data-band', 'uncapped');
  });
});

describe('BudgetLines — one bar per capped line, and no total (#4685)', () => {
  it('draws exactly one bar per capped line and none for any total', () => {
    // The distinction the component must draw: a LINE has a real cap on the wire, so
    // its bar has a genuine denominator and is honest. Two capped lines in, exactly
    // two progressbars out — a third would mean something summed them and drew a bar
    // under a figure no cap governs.
    render(<BudgetLines lines={[mockDirectLine, mockCloudLine]} />);
    expect(screen.getAllByRole('progressbar')).toHaveLength(2);
  });

  it('renders no summed figure of any kind', () => {
    // `CombinedTotal` is deleted, and the sum must not reappear as incidental markup:
    // $412.80 + $171.40 = $584.20 is a figure keyed across two different ledgers
    // (Cognito sub vs canonical users.id), so nothing enforces it.
    render(<BudgetLines lines={[mockDirectLine, mockCloudLine]} />);

    expect(screen.queryByTestId('combined-informational')).not.toBeInTheDocument();
    expect(screen.queryByTestId('combined-total-amount')).not.toBeInTheDocument();
    expect(screen.getByText('Your budget lines').closest('div')?.textContent ?? '').not.toContain('$584.20');
  });
});

describe('BudgetLines — service principals', () => {
  it('renders a service: root row with a service affordance, not as a person', () => {
    // Criterion 10. A `service:`-rooted line is CI or an alarm, not a colleague;
    // rendering it as a person makes per-person cost truth wrong.
    render(<BudgetLineRow line={mockServiceLine} />);

    const affordance = screen.getByTestId('service-principal-affordance');
    expect(affordance).toBeInTheDocument();
    expect(affordance).toHaveAttribute('data-principal-kind', 'service');
  });

  it('renders a human line with no service affordance', () => {
    render(<BudgetLineRow line={mockDirectLine} />);
    expect(screen.queryByTestId('service-principal-affordance')).not.toBeInTheDocument();
  });

  it('derives the affordance from principal_kind, not from the label text', () => {
    // A human can be *called* anything, so the label is not evidence. A line labelled
    // like a bot but flagged `human` must render as a person.
    render(<BudgetLineRow line={{ ...mockDirectLine, label: 'ci-bot' }} />);
    expect(screen.queryByTestId('service-principal-affordance')).not.toBeInTheDocument();
  });
});

describe('BudgetLines — empty state', () => {
  it('renders an empty state when there are no lines', () => {
    render(<BudgetLines lines={[]} />);
    expect(screen.getByTestId('budget-lines-empty')).toBeInTheDocument();
  });
});
