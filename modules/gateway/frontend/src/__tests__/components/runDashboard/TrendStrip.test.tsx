/**
 * Tests for the TrendStrip component.
 *
 * Issue #3771: 7-day trend strip — validates rendering, day click navigation,
 * and the <2 days guard.
 * Issue #3825: numerals above bars, legend, active segment for today,
 * 8-entry daily[] renders 7 bars.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TrendStrip, sliceLast7Days } from '@/components/runDashboard/TrendStrip';
import type { TrendDay } from '@/components/runDashboard/TrendStrip';

// Mock useNavigate
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** 7-entry fixture — no slicing needed */
const sevenDayFixture: TrendDay[] = [
  { date: '2026-07-06', total: 12, completed: 10, failed: 2 },
  { date: '2026-07-07', total: 8, completed: 7, failed: 1 },
  { date: '2026-07-08', total: 15, completed: 12, failed: 3 },
  { date: '2026-07-09', total: 5, completed: 5, failed: 0 },
  { date: '2026-07-10', total: 20, completed: 18, failed: 2 },
  { date: '2026-07-11', total: 0, completed: 0, failed: 0 },
  { date: '2026-07-12', total: 3, completed: 2, failed: 1 },
];

/**
 * 8-entry fixture mirroring real live data from the issue.
 * Today (07-12) has active runs: total(68) > completed(62) + failed(0).
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

const oneDayFixture: TrendDay[] = [
  { date: '2026-07-12', total: 5, completed: 4, failed: 1 },
];

const emptyFixture: TrendDay[] = [];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderTrendStrip(daily: TrendDay[]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TrendStrip daily={daily} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('TrendStrip', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders 7 bars from a 7-entry daily[] fixture', () => {
    renderTrendStrip(sevenDayFixture);

    const strip = screen.getByTestId('trend-strip');
    expect(strip).toBeInTheDocument();

    // Should show 7 clickable day buttons
    const buttons = strip.querySelectorAll('button');
    expect(buttons).toHaveLength(7);

    // Check header text
    expect(screen.getByText('Past 7 days')).toBeInTheDocument();
  });

  it('renders exactly 7 bars from an 8-entry daily[] fixture', () => {
    renderTrendStrip(eightDayFixture);

    const strip = screen.getByTestId('trend-strip');
    const buttons = strip.querySelectorAll('button');
    expect(buttons).toHaveLength(7);

    // Title still says "Past 7 days"
    expect(screen.getByText('Past 7 days')).toBeInTheDocument();

    // The oldest day (07-05) should NOT appear
    expect(screen.queryByTestId('trend-bar-count-2026-07-05')).not.toBeInTheDocument();
    // The second day (07-06) should appear (first visible)
    expect(screen.getByTestId('trend-bar-count-2026-07-06')).toBeInTheDocument();
  });

  it('hides when fewer than 2 days of data', () => {
    const { container } = renderTrendStrip(oneDayFixture);
    expect(container.innerHTML).toBe('');
  });

  it('hides when daily array is empty', () => {
    const { container } = renderTrendStrip(emptyFixture);
    expect(container.innerHTML).toBe('');
  });

  it('day click navigates with since and until params', async () => {
    const user = userEvent.setup();
    renderTrendStrip(sevenDayFixture);

    // Click the first day (2026-07-06)
    const firstButton = screen.getByLabelText(/Mon: 10 succeeded, 2 failed/);
    await user.click(firstButton);

    expect(mockNavigate).toHaveBeenCalledWith(
      '/activity?view=runs&since=2026-07-06&until=2026-07-06',
    );
  });

  it('shows day labels', () => {
    renderTrendStrip(sevenDayFixture);

    // 2026-07-06 is a Monday, 2026-07-12 is a Sunday
    expect(screen.getByText('Mon')).toBeInTheDocument();
    expect(screen.getByText('Sun')).toBeInTheDocument();
  });

  it('displays title attributes with day counts', () => {
    renderTrendStrip(sevenDayFixture);

    // Check that the first bar has a useful title
    const firstButton = screen.getByLabelText(/Mon: 10 succeeded, 2 failed/);
    expect(firstButton).toHaveAttribute(
      'title',
      'Mon: 10 succeeded, 2 failed',
    );
  });

  it('renders numeral (total count) above each bar', () => {
    renderTrendStrip(sevenDayFixture);

    // Each day should have its total as a visible numeral
    expect(screen.getByTestId('trend-bar-count-2026-07-06')).toHaveTextContent('12');
    expect(screen.getByTestId('trend-bar-count-2026-07-09')).toHaveTextContent('5');
    expect(screen.getByTestId('trend-bar-count-2026-07-11')).toHaveTextContent('0');
  });

  it('renders the legend with Succeeded, Failed, Running and unit', () => {
    renderTrendStrip(sevenDayFixture);

    const legend = screen.getByTestId('trend-strip-legend');
    expect(legend).toBeInTheDocument();
    expect(legend).toHaveTextContent('Succeeded');
    expect(legend).toHaveTextContent('Failed');
    expect(legend).toHaveTextContent('Running');
    expect(legend).toHaveTextContent('runs per day');
  });

  it("shows active segment for today's bucket when total > completed + failed", () => {
    renderTrendStrip(eightDayFixture);

    // Today = 2026-07-12: total(68) - completed(62) - failed(0) = 6 active
    const activeSegment = screen.getByTestId('trend-bar-active-2026-07-12');
    expect(activeSegment).toBeInTheDocument();

    // aria-label should include running count
    const todayButton = screen.getByLabelText(/Sun: 62 succeeded, 0 failed, 6 running/);
    expect(todayButton).toBeInTheDocument();
  });

  it('does NOT show active segment for past days even if total > completed + failed', () => {
    // Use a fixture where a past day has total > completed + failed (stale data scenario)
    const staleFixture: TrendDay[] = [
      { date: '2026-07-10', total: 10, completed: 8, failed: 1 }, // residue of 1
      { date: '2026-07-11', total: 10, completed: 8, failed: 1 }, // residue of 1
      { date: '2026-07-12', total: 10, completed: 8, failed: 1 }, // today: active=1
    ];
    renderTrendStrip(staleFixture);

    // Only today should have the active segment
    expect(screen.queryByTestId('trend-bar-active-2026-07-10')).not.toBeInTheDocument();
    expect(screen.queryByTestId('trend-bar-active-2026-07-11')).not.toBeInTheDocument();
    expect(screen.getByTestId('trend-bar-active-2026-07-12')).toBeInTheDocument();
  });
});

describe('sliceLast7Days', () => {
  it('returns all entries when length <= 7', () => {
    expect(sliceLast7Days(sevenDayFixture)).toHaveLength(7);
    expect(sliceLast7Days(oneDayFixture)).toHaveLength(1);
    expect(sliceLast7Days(emptyFixture)).toHaveLength(0);
  });

  it('returns last 7 entries when length > 7', () => {
    const result = sliceLast7Days(eightDayFixture);
    expect(result).toHaveLength(7);
    // Should drop the oldest (2026-07-05)
    expect(result[0].date).toBe('2026-07-06');
    expect(result[6].date).toBe('2026-07-12');
  });
});
