/**
 * Tests for the consolidated cost formatter (issue #4207).
 *
 * The assertion that matters most in this file is that `unknown` NEVER renders as
 * a currency string. Five separate formatters used to exist and the most widely
 * used of them coerced null to `$0.00`, so "we don't know what this cost" and
 * "this was free" rendered identically. Finance and ops read the fabricated zeros
 * as real ones.
 *
 * The three-valued vocabulary is deliberate and the two zero-dollar cases are
 * different news:
 *   - `none_incurred` — a measured zero. `$0.00` is honest.
 *   - `unknown`       — no measurement. Must be a non-currency indicator.
 */
import { describe, it, expect } from 'vitest';
import {
  COST_SCOPE_LABEL,
  NO_DATA_INDICATOR,
  costTooltip,
  describeUnknownReason,
  formatAmount,
  formatCost,
  formatCostFigure,
  formatRunCost,
} from '@/utils/cost';

describe('formatCost — absence is never a number', () => {
  it('renders null as a non-currency indicator, NOT $0.00', () => {
    // The bug this story exists to end.
    expect(formatCost(null)).toBe(NO_DATA_INDICATOR);
    expect(formatCost(null)).not.toBe('$0.00');
    expect(formatCost(null)).not.toMatch(/\$/);
  });

  it('renders undefined as a non-currency indicator', () => {
    expect(formatCost(undefined)).toBe(NO_DATA_INDICATOR);
    expect(formatCost(undefined)).not.toMatch(/\$/);
  });

  it('renders NaN as a non-currency indicator', () => {
    // A NaN reaching a formatter is a data-shape bug; rendering "$NaN" or "$0.00"
    // both hide it, and the second one hides it as a plausible figure.
    expect(formatCost(NaN)).toBe(NO_DATA_INDICATOR);
  });

  it('renders a real zero as $0.00', () => {
    // A genuine zero is a measurement and must still read as one.
    expect(formatCost(0)).toBe('$0.00');
  });

  it('distinguishes a real zero from a missing value', () => {
    expect(formatCost(0)).not.toBe(formatCost(null));
  });
});

describe('formatAmount — sub-cent precision', () => {
  it('uses four decimals below a cent', () => {
    // Most individual agent calls are sub-cent. A flat toFixed(2) renders them
    // "$0.00", which is the same lie by a different route.
    expect(formatAmount(0.0012)).toBe('$0.0012');
    expect(formatAmount(0.000001)).toBe('$0.0000');
  });

  it('uses two decimals at or above a cent', () => {
    expect(formatAmount(0.01)).toBe('$0.01');
    expect(formatAmount(1.5)).toBe('$1.50');
    expect(formatAmount(1234.5)).toBe('$1234.50');
  });

  it('renders zero as $0.00', () => {
    expect(formatAmount(0)).toBe('$0.00');
  });
});

describe('formatCostFigure — three-valued', () => {
  it('renders unknown as a non-currency indicator', () => {
    const result = formatCostFigure({ status: 'unknown', reason: 'no_usage_rows' });
    expect(result).toBe(NO_DATA_INDICATOR);
    expect(result).not.toMatch(/\$/);
  });

  it('renders none_incurred as $0.00 — a verified zero', () => {
    expect(formatCostFigure({ status: 'none_incurred', amount_usd: '0' })).toBe('$0.00');
  });

  it('renders known with its amount', () => {
    expect(formatCostFigure({ status: 'known', amount_usd: '1.500000' })).toBe('$1.50');
  });

  it('preserves sub-cent precision through the string wire format', () => {
    // Numeric(10,6) is serialised as a string precisely so this survives.
    expect(formatCostFigure({ status: 'known', amount_usd: '0.001234' })).toBe('$0.0012');
  });

  it('distinguishes none_incurred from unknown', () => {
    const measured = formatCostFigure({ status: 'none_incurred', amount_usd: '0' });
    const absent = formatCostFigure({ status: 'unknown', reason: 'not_started' });
    expect(measured).not.toBe(absent);
    expect(measured).toBe('$0.00');
    expect(absent).toBe(NO_DATA_INDICATOR);
  });

  it('treats status as authoritative over a stray amount on unknown', () => {
    // Defensive, but this is the exact seam where an unknown carrying 0 would
    // become "$0.00" again.
    expect(formatCostFigure({ status: 'unknown', amount_usd: '0', reason: 'not_started' })).toBe(NO_DATA_INDICATOR);
  });

  it('renders a null or missing figure as the no-data indicator', () => {
    expect(formatCostFigure(null)).toBe(NO_DATA_INDICATOR);
    expect(formatCostFigure(undefined)).toBe(NO_DATA_INDICATOR);
  });

  it('renders a known figure with a null amount as no-data rather than $0.00', () => {
    // Contradictory payload; degrade to "unknown", never invent a zero.
    expect(formatCostFigure({ status: 'known', amount_usd: null })).toBe(NO_DATA_INDICATOR);
  });

  it('renders an unparseable amount as no-data', () => {
    expect(formatCostFigure({ status: 'known', amount_usd: 'not-a-number' })).toBe(NO_DATA_INDICATOR);
  });
});

describe('formatRunCost — pending vs no-data', () => {
  it('renders zero on an in-progress run as pending, not $0.00', () => {
    // Cost has not been backfilled yet; the run was not free.
    expect(formatRunCost(0, 'in_progress')).toBe('pending');
  });

  it('renders zero on a completed run as $0.00', () => {
    expect(formatRunCost(0, 'complete')).toBe('$0.00');
  });

  it('renders null as the no-data indicator regardless of status', () => {
    expect(formatRunCost(null, 'in_progress')).toBe(NO_DATA_INDICATOR);
    expect(formatRunCost(null, 'complete')).toBe(NO_DATA_INDICATOR);
  });

  it('renders a real amount on an in-progress run', () => {
    expect(formatRunCost(0.0012, 'in_progress')).toBe('$0.0012');
  });

  it('works with no status argument', () => {
    expect(formatRunCost(1.5)).toBe('$1.50');
    expect(formatRunCost(0)).toBe('$0.00');
  });
});

describe('scope label', () => {
  it('names what is excluded, not just what is included', () => {
    // "Agent run costs" alone would not tell a reader build/infra is missing.
    expect(COST_SCOPE_LABEL).toMatch(/exclude/i);
    expect(COST_SCOPE_LABEL).toMatch(/infra/i);
  });

  it('is present on every tooltip', () => {
    expect(costTooltip({ status: 'known', amount_usd: '1.00' })).toContain(COST_SCOPE_LABEL);
    expect(costTooltip({ status: 'unknown', reason: 'not_started' })).toContain(COST_SCOPE_LABEL);
    expect(costTooltip(null)).toContain(COST_SCOPE_LABEL);
  });

  it('prefers the scope the API reported over the local default', () => {
    // The backend owns the label; a drifting local copy would misdescribe it.
    const tooltip = costTooltip({ status: 'known', amount_usd: '1.00', scope: 'custom scope from api' });
    expect(tooltip).toContain('custom scope from api');
  });
});

describe('costTooltip — partial totals', () => {
  it('says a partial total is a lower bound', () => {
    // A partial total presented as a total is how decisions get made on wrong
    // numbers.
    const tooltip = costTooltip({ status: 'known', amount_usd: '2.00' }, { partial: true });
    expect(tooltip).toMatch(/lower bound/i);
  });

  it('does not claim partial when the aggregate is complete', () => {
    const tooltip = costTooltip({ status: 'known', amount_usd: '2.00' }, { partial: false });
    expect(tooltip).not.toMatch(/lower bound/i);
  });

  it('explains why a figure is unknown', () => {
    const tooltip = costTooltip({ status: 'unknown', reason: 'not_costable' });
    expect(tooltip).toMatch(/nothing to bill/i);
  });
});

describe('describeUnknownReason', () => {
  it('maps each known reason to an explanation', () => {
    expect(describeUnknownReason('no_usage_rows')).toMatch(/no metered usage/i);
    expect(describeUnknownReason('not_started')).toMatch(/has not run yet/i);
    expect(describeUnknownReason('not_costable')).toMatch(/nothing to bill/i);
    expect(describeUnknownReason('non_gateway_path')).toMatch(/directly/i);
  });

  it('degrades an unmapped reason to a generic sentence, never a blank', () => {
    // The API and the SPA deploy independently, so an unfamiliar reason is
    // normal. A blank tooltip would read as a UI bug.
    expect(describeUnknownReason('some_future_reason')).toBeTruthy();
    expect(describeUnknownReason(null)).toBeTruthy();
    expect(describeUnknownReason(undefined)).toBeTruthy();
  });
});

describe('no-data indicator', () => {
  it('is not a currency string', () => {
    // The single invariant the whole module rests on.
    expect(NO_DATA_INDICATOR).not.toMatch(/\$/);
    expect(NO_DATA_INDICATOR).not.toMatch(/\d/);
  });
});
