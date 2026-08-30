/**
 * Tests for the envelope lines and the combined total — Issue #4402 (U-5).
 *
 * The load-bearing assertions here are the negative ones. Criterion 4 of the issue is
 * that the combined direct+cloud figure renders with **no** `role="progressbar"` and
 * **no** `x / y` denominator, because no cap governs that number — a bar or a
 * denominator would have users planning against an invented ceiling. A test that only
 * checked the figure appeared would pass just as happily with a bar next to it, so the
 * absence is asserted explicitly.
 *
 * Fixtures come from `mocks/data/budgetSpend.ts`, transcribed from
 * `src/budget/schemas.py` — never from `src/types/budget.ts` (the #3675 guard).
 */

import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { BudgetLines, BudgetLineRow, CombinedTotal } from '@/components/budget/BudgetLines';
import { mockBudgetEnvelope, mockDirectLine, mockCloudLine, mockServiceLine, mockUncappedLine } from '@/mocks/data/budgetSpend';
import type { BudgetLine } from '@/types/budget';

/** Build a line at a given utilisation + band, as the SERVER would report it. */
function lineAt(utilization_pct: number, band: BudgetLine['band']): BudgetLine {
  return { ...mockDirectLine, utilization_pct, band };
}

describe('BudgetLines — per-line rows', () => {
  it('renders each line with its own cap, spend, headroom and utilisation', () => {
    render(<BudgetLines lines={[mockDirectLine, mockCloudLine]} combined={null} />);

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

describe('BudgetLines — combined informational total', () => {
  const combined = mockBudgetEnvelope.combined_informational!;

  it('renders the combined figure with NO progressbar anywhere', () => {
    // Criterion 4. No cap governs this number, so a bar would imply a ceiling that
    // does not exist. Rendered without any line rows so the only candidate bar would
    // be the combined figure's own.
    render(<CombinedTotal combined={combined} />);

    expect(screen.getByTestId('combined-total-amount')).toHaveTextContent('$584.20');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('renders the combined figure with no "x / y" denominator', () => {
    render(<CombinedTotal combined={combined} />);

    const region = screen.getByTestId('combined-informational');
    // No slash-joined pair of figures, which is the shape a denominator takes.
    expect(region.textContent).not.toMatch(/\$[\d,.]+\s*\/\s*\$?[\d,.]+/);
    // And neither of the two caps leaks in as a denominator.
    expect(region.textContent).not.toContain('$600.00');
    expect(region.textContent).not.toContain('$200.00');
  });

  it('captions the combined figure as not a budget, using the server note', () => {
    render(<CombinedTotal combined={combined} />);
    expect(screen.getByText(combined.note)).toBeInTheDocument();
  });

  it('keeps the per-line bars while adding none for the combined total', () => {
    // The distinction the screen must draw: a LINE has a real cap on the wire, so its
    // bar is honest; the combined total has none, so it gets no bar. Two capped lines
    // in, exactly two progressbars out.
    render(<BudgetLines lines={[mockDirectLine, mockCloudLine]} combined={combined} />);
    expect(screen.getAllByRole('progressbar')).toHaveLength(2);
  });

  it('omits the combined section entirely when there is nothing to combine', () => {
    render(<BudgetLines lines={[mockDirectLine]} combined={null} />);
    expect(screen.queryByTestId('combined-informational')).not.toBeInTheDocument();
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
    render(<BudgetLines lines={[]} combined={null} />);
    expect(screen.getByTestId('budget-lines-empty')).toBeInTheDocument();
  });
});
