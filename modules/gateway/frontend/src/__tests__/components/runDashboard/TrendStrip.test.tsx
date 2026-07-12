/**
 * Tests for the TrendStrip component.
 *
 * Issue #3771: 7-day trend strip — validates rendering, day click navigation,
 * and the <2 days guard.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TrendStrip } from '@/components/runDashboard/TrendStrip';
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

const sevenDayFixture: TrendDay[] = [
  { date: '2026-07-06', total: 12, completed: 10, failed: 2 },
  { date: '2026-07-07', total: 8, completed: 7, failed: 1 },
  { date: '2026-07-08', total: 15, completed: 12, failed: 3 },
  { date: '2026-07-09', total: 5, completed: 5, failed: 0 },
  { date: '2026-07-10', total: 20, completed: 18, failed: 2 },
  { date: '2026-07-11', total: 0, completed: 0, failed: 0 },
  { date: '2026-07-12', total: 3, completed: 2, failed: 1 },
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

  it('renders 7 bars from a daily[] fixture', () => {
    renderTrendStrip(sevenDayFixture);

    const strip = screen.getByTestId('trend-strip');
    expect(strip).toBeInTheDocument();

    // Should show 7 clickable day buttons
    const buttons = strip.querySelectorAll('button');
    expect(buttons).toHaveLength(7);

    // Check header text
    expect(screen.getByText('Past 7 days')).toBeInTheDocument();
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
});
