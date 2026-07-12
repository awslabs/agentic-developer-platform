/**
 * Tests for the WeekSummary component.
 *
 * Issue #3771: Week summary line — validates cost display, run count,
 * null spend handling, and hide-when-empty behavior.
 * Issue #3825: Uses sliceLast7Days — 8-entry daily[] sums only the last 7;
 * label says "Past 7 days:" not "This week:".
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WeekSummary } from '@/components/runDashboard/WeekSummary';
import type { TrendDay } from '@/components/runDashboard/TrendStrip';
import type { RunStatsSpend } from '@/services/runStats';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const dailyWithData: TrendDay[] = [
  { date: '2026-07-06', total: 12, completed: 10, failed: 2 },
  { date: '2026-07-07', total: 8, completed: 7, failed: 1 },
  { date: '2026-07-08', total: 15, completed: 12, failed: 3 },
  { date: '2026-07-09', total: 5, completed: 5, failed: 0 },
  { date: '2026-07-10', total: 20, completed: 18, failed: 2 },
  { date: '2026-07-11', total: 0, completed: 0, failed: 0 },
  { date: '2026-07-12', total: 3, completed: 2, failed: 1 },
];

/**
 * 8-entry fixture mirroring live data from issue #3825.
 * Total across all 8 = 799; total across last 7 = 758.
 */
const eightDayFixture: TrendDay[] = [
  { date: '2026-07-05', total: 41, completed: 41, failed: 0 },
  { date: '2026-07-06', total: 98, completed: 95, failed: 3 },
  { date: '2026-07-07', total: 160, completed: 157, failed: 3 },
  { date: '2026-07-08', total: 111, completed: 109, failed: 2 },
  { date: '2026-07-09', total: 115, completed: 112, failed: 3 },
  { date: '2026-07-10', total: 46, completed: 45, failed: 1 },
  { date: '2026-07-11', total: 160, completed: 155, failed: 5 },
  { date: '2026-07-12', total: 68, completed: 62, failed: 0 },
];

const spendData: RunStatsSpend = {
  total_cost_usd: 45.67,
  total_tokens: 200000,
  total_calls: 100,
};

const zeroDays: TrendDay[] = [
  { date: '2026-07-11', total: 0, completed: 0, failed: 0 },
  { date: '2026-07-12', total: 0, completed: 0, failed: 0 },
];

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('WeekSummary', () => {
  it('displays formatted cost and total run count with "Past 7 days" label', () => {
    render(<WeekSummary daily={dailyWithData} spend={spendData} />);

    const el = screen.getByTestId('week-summary');
    expect(el).toBeInTheDocument();
    expect(el).toHaveTextContent('Past 7 days:');
    expect(el).toHaveTextContent('$45.67');
    // Total runs: 12+8+15+5+20+0+3 = 63
    expect(el).toHaveTextContent('63 runs');
  });

  it('sums only the last 7 entries when daily[] has 8 entries', () => {
    render(<WeekSummary daily={eightDayFixture} spend={spendData} />);

    const el = screen.getByTestId('week-summary');
    // Last 7 entries total: 98+160+111+115+46+160+68 = 758 (not 799)
    expect(el).toHaveTextContent('758 runs');
    expect(el).toHaveTextContent('Past 7 days:');
  });

  it('shows "—" when spend is null', () => {
    render(<WeekSummary daily={dailyWithData} spend={null} />);

    const el = screen.getByTestId('week-summary');
    expect(el).toHaveTextContent('—');
    // Still shows run count
    expect(el).toHaveTextContent('63 runs');
  });

  it('uses singular "run" when total is 1', () => {
    const singleRunDay: TrendDay[] = [
      { date: '2026-07-12', total: 1, completed: 1, failed: 0 },
    ];
    render(<WeekSummary daily={singleRunDay} spend={spendData} />);

    const el = screen.getByTestId('week-summary');
    expect(el).toHaveTextContent('1 run');
    expect(el).not.toHaveTextContent('1 runs');
  });

  it('hides when both total runs is 0 and spend is null', () => {
    const { container } = render(<WeekSummary daily={zeroDays} spend={null} />);
    expect(container.innerHTML).toBe('');
  });

  it('shows when spend is available even with 0 runs', () => {
    render(<WeekSummary daily={zeroDays} spend={spendData} />);

    const el = screen.getByTestId('week-summary');
    expect(el).toHaveTextContent('$45.67');
    expect(el).toHaveTextContent('0 runs');
  });

  it('shows when there are runs but no spend data', () => {
    render(<WeekSummary daily={dailyWithData} spend={null} />);

    const el = screen.getByTestId('week-summary');
    expect(el).toBeInTheDocument();
  });

  it('does NOT use "This week" label (old wording)', () => {
    render(<WeekSummary daily={dailyWithData} spend={spendData} />);

    const el = screen.getByTestId('week-summary');
    expect(el).not.toHaveTextContent('This week:');
  });
});
