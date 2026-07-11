/**
 * Tests for the AgentRunDashboard page.
 *
 * Issue #3633: Agent Run Dashboard — validates tiles, empty state,
 * loading skeleton, cost null handling, and failure row links.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import AgentRunDashboard from '@/pages/AgentRunDashboard';
import type { RunStatsResponse } from '@/services/runStats';

// Mock the service
vi.mock('@/services/runStats', () => ({
  getRunStats: vi.fn(),
}));

import { getRunStats } from '@/services/runStats';

const mockGetRunStats = getRunStats as ReturnType<typeof vi.fn>;

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
// Test fixtures
// ---------------------------------------------------------------------------

const mockStatsWithData: RunStatsResponse = {
  window_days: 7,
  active_runs: [
    {
      invocation_id: 'inv-active-001',
      invoked_at: '2026-07-11T11:00:00Z',
      topic: 'Deploy gateway',
      persona: 'developer',
      repo: 'acme/api',
    },
  ],
  today: {
    total: 10,
    completed: 7,
    failed: 2,
    active: 1,
  },
  daily: [],
  by_persona: [],
  recent_failures: [
    {
      invocation_id: 'inv-fail-001',
      topic: 'Upgrade dependencies',
      invoked_at: '2026-07-11T10:00:00Z',
      persona: 'developer',
      repo: 'acme/api',
      error_message: null,
    },
    {
      invocation_id: 'inv-fail-002',
      topic: 'Refactor auth module',
      invoked_at: '2026-07-11T08:00:00Z',
      persona: 'architect',
      repo: 'acme/web',
      error_message: null,
    },
  ],
  top_repos: [],
  spend: { total_cost_usd: 12.34, total_tokens: 50000, total_calls: 20 },
};

const mockStatsEmpty: RunStatsResponse = {
  window_days: 7,
  active_runs: [],
  today: {
    total: 0,
    completed: 0,
    failed: 0,
    active: 0,
  },
  daily: [],
  by_persona: [],
  recent_failures: [],
  top_repos: [],
  spend: null,
};

const mockStatsNullSpend: RunStatsResponse = {
  window_days: 7,
  active_runs: [
    {
      invocation_id: 'inv-active-001',
      invoked_at: '2026-07-11T11:00:00Z',
      topic: 'Running task',
      persona: 'developer',
      repo: 'acme/api',
    },
  ],
  today: {
    total: 5,
    completed: 3,
    failed: 1,
    active: 1,
  },
  daily: [],
  by_persona: [],
  recent_failures: [],
  top_repos: [],
  spend: null,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  });
}

function renderDashboard() {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AgentRunDashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AgentRunDashboard Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetRunStats.mockResolvedValue(mockStatsWithData);
  });

  it('renders all 4 tiles with correct values', async () => {
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Running now')).toBeInTheDocument();
    });

    expect(screen.getByText('Running now')).toBeInTheDocument();
    expect(screen.getByText('Failed today')).toBeInTheDocument();
    expect(screen.getByText('Spend today')).toBeInTheDocument();
    expect(screen.getByText('Succeeded today')).toBeInTheDocument();

    // Check values
    // Active runs count = 1 (array length)
    expect(screen.getByLabelText(/Running now: 1/)).toBeInTheDocument();
    // Failed = 2
    expect(screen.getByLabelText(/Failed today: 2/)).toBeInTheDocument();
    // Spend = $12.34
    expect(screen.getByText('$12.34')).toBeInTheDocument();
    // Succeeded = 7
    expect(screen.getByLabelText(/Succeeded today: 7/)).toBeInTheDocument();
  });

  it('renders empty state when all counts are zero', async () => {
    mockGetRunStats.mockResolvedValue(mockStatsEmpty);
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('No agent runs yet')).toBeInTheDocument();
    });

    // Tiles should not be visible
    expect(screen.queryByText('Running now')).not.toBeInTheDocument();
    expect(screen.queryByText('Failed today')).not.toBeInTheDocument();
  });

  it('shows "—" when spend is null (not "$0.00")', async () => {
    mockGetRunStats.mockResolvedValue(mockStatsNullSpend);
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Spend today')).toBeInTheDocument();
    });

    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.getByText('Cost data temporarily unavailable')).toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('renders failure rows with correct links', async () => {
    const user = userEvent.setup();
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Upgrade dependencies')).toBeInTheDocument();
    });

    expect(screen.getByText('Refactor auth module')).toBeInTheDocument();

    // Click a failure row
    await user.click(screen.getByText('Upgrade dependencies'));

    expect(mockNavigate).toHaveBeenCalledWith('/activity?id=inv-fail-001');
  });

  it('renders loading skeleton while data is being fetched', () => {
    // Never resolves
    mockGetRunStats.mockReturnValue(new Promise(() => {}));
    renderDashboard();

    expect(screen.getByTestId('loading-skeleton')).toBeInTheDocument();
  });

  it('tile click navigates to correct Activity page URLs', async () => {
    const user = userEvent.setup();
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Running now')).toBeInTheDocument();
    });

    // Click "Running now" tile
    await user.click(screen.getByLabelText(/Running now: 1/));
    expect(mockNavigate).toHaveBeenCalledWith('/activity?status=in_progress');

    mockNavigate.mockClear();

    // Click "Failed today" tile
    await user.click(screen.getByLabelText(/Failed today: 2/));
    expect(mockNavigate).toHaveBeenCalledWith('/activity?status=failed&since=today');

    mockNavigate.mockClear();

    // Click "Succeeded today" tile
    await user.click(screen.getByLabelText(/Succeeded today: 7/));
    expect(mockNavigate).toHaveBeenCalledWith('/activity?status=complete&since=today');
  });

  it('shows error state with retry button on API failure', async () => {
    const user = userEvent.setup();
    mockGetRunStats.mockRejectedValueOnce(new Error('Network failure'));
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Network failure')).toBeInTheDocument();
    });

    expect(screen.getByText('Retry')).toBeInTheDocument();

    // Click retry
    mockGetRunStats.mockResolvedValueOnce(mockStatsWithData);
    await user.click(screen.getByText('Retry'));

    await waitFor(() => {
      // Page issues two queries (7-day stats + 1-day spend); retry refetches
      // the failed one, so total calls is at least 3.
      expect(mockGetRunStats.mock.calls.length).toBeGreaterThanOrEqual(3);
    });
  });

  it('renders page title and subtitle', async () => {
    renderDashboard();

    expect(screen.getByText('Agent Runs')).toBeInTheDocument();
    expect(screen.getByText('Overview of your agent run activity')).toBeInTheDocument();
  });
});
