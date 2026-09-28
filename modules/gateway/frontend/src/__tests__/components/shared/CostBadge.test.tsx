/**
 * Tests for the shared cost badges (issue #4400).
 *
 * The badges were local to `pages/AgentActivity.tsx` and are now shared with the
 * budget drill-down. What these tests protect is a single invariant that the
 * whole three-valued cost design exists to hold:
 *
 *   **An absent cost never renders as a currency amount.**
 *
 * A `$0.00` where the truth is "we have no ledger row for this" tells an operator
 * the work was free. Every assertion below is ultimately about that, plus its
 * aggregate cousin: a *partial* total must not read as an exact one.
 *
 * Formatting rules themselves (sub-cent precision, the `'—'` convention) belong
 * to `utils/cost.ts` and are tested in `utils/cost.test.ts`. These tests cover
 * what the badges add: which styling branch is taken, and whether the tooltip and
 * the partial marker are actually present.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CostBadge, ChainCostBadge, CostFigureBadge } from '@/components/shared/CostBadge';
import { COST_SCOPE_LABEL, NO_DATA_INDICATOR } from '@/utils/cost';
import type { CostFigure } from '@/utils/cost';
import type { InvocationItem } from '@/types/activity';

function invocation(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'evt-1',
    channel: 'github',
    status: 'complete',
    invoked_at: '2026-06-15T10:00:00Z',
    ...overrides,
  } as InvocationItem;
}

describe('CostBadge', () => {
  it('renders a known cost as a monospaced amount with a usage tooltip', () => {
    render(<CostBadge item={invocation({ total_cost_usd: 1.5, call_count: 12, total_tokens: 3400 })} />);
    const badge = screen.getByText('$1.50');
    expect(badge).toHaveClass('font-mono');
    // The scope disclaimer rides along on every figure (R-N5c) — a number that
    // silently excludes build/infra cost reads as the total.
    expect(badge.getAttribute('title')).toBe(`12 calls, 3400 tokens — ${COST_SCOPE_LABEL}`);
  });

  it('renders an absent cost as the no-data indicator, never as $0.00', () => {
    // The core invariant. `null` means "not metered / no usage row yet".
    render(<CostBadge item={invocation({ total_cost_usd: null })} />);
    expect(screen.getByText(NO_DATA_INDICATOR)).toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('explains the no-data case rather than leaving a bare dash', () => {
    // An unexplained '—' reads as a UI bug.
    render(<CostBadge item={invocation({ total_cost_usd: null })} />);
    expect(screen.getByText(NO_DATA_INDICATOR).getAttribute('title')).toBe(COST_SCOPE_LABEL);
  });

  it('renders a zero on an in-progress run as pending, not as free', () => {
    // Cost is backfilled after the run, so a running row at zero has not been
    // measured yet. Italic because it is a statement about time, not money.
    render(<CostBadge item={invocation({ total_cost_usd: 0, status: 'in_progress' })} />);
    const badge = screen.getByText('pending');
    expect(badge).toHaveClass('italic');
  });

  it('renders a zero on a finished run as a real $0.00', () => {
    // Same number, opposite meaning: a completed run measured at zero is free.
    render(<CostBadge item={invocation({ total_cost_usd: 0, status: 'complete' })} />);
    expect(screen.getByText('$0.00')).toBeInTheDocument();
  });

  it('tolerates missing call and token counts in the tooltip', () => {
    render(<CostBadge item={invocation({ total_cost_usd: 2, call_count: null, total_tokens: null })} />);
    expect(screen.getByText('$2.00').getAttribute('title')).toBe(`0 calls, 0 tokens — ${COST_SCOPE_LABEL}`);
  });
});

describe('ChainCostBadge', () => {
  it('renders a known chain total with the scope tooltip', () => {
    render(<ChainCostBadge cost={12.34} />);
    const badge = screen.getByText('$12.34');
    expect(badge).toHaveClass('font-mono');
    expect(badge.getAttribute('title')).toBe(COST_SCOPE_LABEL);
  });

  it('mutes an unknown chain total instead of claiming zero', () => {
    render(<ChainCostBadge cost={null} />);
    const badge = screen.getByText(NO_DATA_INDICATOR);
    expect(badge).toHaveClass('text-gray-400');
    expect(badge).not.toHaveClass('font-mono');
  });
});

describe('CostFigureBadge', () => {
  const known: CostFigure = { status: 'known', amount_usd: '4.250000' };

  it('renders a known figure from its string amount', () => {
    // Amounts cross the wire as strings so sub-cent precision survives JSON.
    render(<CostFigureBadge figure={known} />);
    expect(screen.getByText('$4.25')).toBeInTheDocument();
  });

  it('preserves sub-cent amounts instead of rounding them to nothing', () => {
    render(<CostFigureBadge figure={{ status: 'known', amount_usd: '0.000523' }} />);
    expect(screen.getByText('$0.0005')).toBeInTheDocument();
  });

  it('renders a verified zero as $0.00', () => {
    // `none_incurred` is a measurement — the run made no model calls. $0.00 is
    // the honest rendering, and the reason the status is on the wire at all.
    render(<CostFigureBadge figure={{ status: 'none_incurred', amount_usd: '0.000000' }} />);
    expect(screen.getByText('$0.00')).toBeInTheDocument();
    expect(screen.getByText('$0.00')).toHaveAttribute('data-cost-status', 'none_incurred');
  });

  it('renders an unknown figure as the no-data indicator', () => {
    render(<CostFigureBadge figure={{ status: 'unknown', reason: 'no_usage_rows' }} />);
    expect(screen.getByText(NO_DATA_INDICATOR)).toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('treats the status as authoritative over a malformed amount', () => {
    // The exact seam where the old bug would come back: an `unknown` that somehow
    // carries a zero amount must still render '—'. The server-side validator
    // forbids this shape, but the client does not get to assume that.
    render(<CostFigureBadge figure={{ status: 'unknown', amount_usd: '0', reason: 'no_usage_rows' }} />);
    expect(screen.getByText(NO_DATA_INDICATOR)).toBeInTheDocument();
  });

  it('explains why a figure is unknown', () => {
    render(<CostFigureBadge figure={{ status: 'unknown', reason: 'no_usage_rows' }} />);
    expect(screen.getByText(NO_DATA_INDICATOR).getAttribute('title')).toContain(
      'No metered usage recorded',
    );
  });

  it('degrades an unfamiliar unknown reason to a generic explanation', () => {
    // The API and the SPA deploy independently, so a new reason string is normal;
    // it must not produce an empty tooltip.
    render(<CostFigureBadge figure={{ status: 'unknown', reason: 'some_new_reason' }} />);
    expect(screen.getByText(NO_DATA_INDICATOR).getAttribute('title')).toContain(
      'Cost data unavailable',
    );
  });

  it('renders a missing figure as unknown rather than crashing', () => {
    render(<CostFigureBadge figure={null} />);
    const badge = screen.getByText(NO_DATA_INDICATOR);
    expect(badge).toHaveAttribute('data-cost-status', 'unknown');
  });

  it('marks a partial total visibly, not only in the tooltip', () => {
    // A partial subtotal is a LOWER BOUND. Someone reading the number at a glance
    // has to see that it is incomplete without hovering.
    render(<CostFigureBadge figure={{ status: 'known', amount_usd: '14.750000', partial: true }} />);
    expect(screen.getByText('+')).toBeInTheDocument();
    expect(screen.getByText(/or more — partial total/)).toBeInTheDocument();
  });

  it('says in the tooltip that a partial total is a lower bound', () => {
    render(<CostFigureBadge figure={{ status: 'known', amount_usd: '14.750000', partial: true }} />);
    const badge = screen.getByText('$14.75');
    expect(badge.getAttribute('title')).toContain('lower bound');
    expect(badge).toHaveAttribute('data-cost-partial', 'true');
  });

  it('reads partial from the figure without the caller having to pass it', () => {
    // The regression this closes: `costTooltip` took `partial` as an option, so a
    // caller who did not know to pass it rendered a partial total as an exact one.
    // The flag now travels on the figure.
    const { container } = render(<CostFigureBadge figure={{ status: 'known', amount_usd: '1.000000', partial: true }} />);
    expect(container.querySelector('[data-cost-partial="true"]')).not.toBeNull();
  });

  it('lets an explicit partial prop override the figure', () => {
    render(<CostFigureBadge figure={{ status: 'known', amount_usd: '1.000000', partial: false }} partial />);
    expect(screen.getByText('$1.00')).toHaveAttribute('data-cost-partial', 'true');
  });

  it('does not mark a fully measured total as partial', () => {
    render(<CostFigureBadge figure={known} />);
    expect(screen.getByText('$4.25')).toHaveAttribute('data-cost-partial', 'false');
    expect(screen.queryByText('+')).not.toBeInTheDocument();
  });

  it('omits the partial marker when there is no amount to qualify', () => {
    // '—+' would be nonsense; the tooltip still carries the explanation.
    render(<CostFigureBadge figure={{ status: 'unknown', reason: 'no_usage_rows', partial: true }} />);
    expect(screen.queryByText('+')).not.toBeInTheDocument();
    expect(screen.getByText(NO_DATA_INDICATOR).getAttribute('title')).toContain('lower bound');
  });

  it('always carries the scope disclaimer', () => {
    for (const figure of [known, { status: 'unknown' } as CostFigure]) {
      const { unmount } = render(<CostFigureBadge figure={figure} />);
      expect(screen.getByTitle(new RegExp(COST_SCOPE_LABEL))).toBeInTheDocument();
      unmount();
    }
  });

  it('honours a caller-supplied className for context-specific styling', () => {
    render(<CostFigureBadge figure={known} className="text-lg font-bold" />);
    const badge = screen.getByText('$4.25');
    expect(badge).toHaveClass('text-lg', 'font-bold');
    expect(badge).not.toHaveClass('font-mono');
  });
});
