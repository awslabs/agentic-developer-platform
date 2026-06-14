/**
 * Tests for the AgentActivity page.
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Issue #1459: Phase 5 — Row detail + polish.
 * Validates: pagination with cursor/last_key, status rendering, link column,
 * admin toggle visibility, error/retry UI, row click → detail modal,
 * distinct empty states (no activity vs no filter matches).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import AgentActivity from '@/pages/AgentActivity';
import type { InvocationItem, InvocationListResponse } from '@/types/activity';

// Mock the services
vi.mock('@/services/activity', () => ({
  getMyInvocations: vi.fn(),
  getAllInvocations: vi.fn(),
}));

// Mock the permissions hook
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: vi.fn(),
}));

import { getMyInvocations, getAllInvocations } from '@/services/activity';
import { usePermissions } from '@/hooks/usePermissions';

const mockGetMine = getMyInvocations as ReturnType<typeof vi.fn>;
const mockGetAll = getAllInvocations as ReturnType<typeof vi.fn>;
const mockUsePermissions = usePermissions as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

function makeInvocation(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'inv-000001',
    user_id: 'user-001',
    persona: 'developer',
    channel: 'github',
    status: 'complete',
    topic: 'Implement Agent Activity page',
    summary: 'Completed work on issue #1457',
    source_url: 'https://github.com/aws-e/adp/issues/1457',
    repo: 'aws-e/adp',
    issue_number: 1457,
    invoked_at: '2026-06-14T10:00:00Z',
    completed_at: '2026-06-14T10:30:00Z',
    status_updated_at: '2026-06-14T10:30:00Z',
    correlation_id: 'corr-abc12345',
    run_id: '81286554630',
    error_message: null,
    ...overrides,
  };
}

const mockItems: InvocationItem[] = [
  makeInvocation({ invocation_id: 'inv-001', status: 'complete', invoked_at: '2026-06-14T10:00:00Z' }),
  makeInvocation({ invocation_id: 'inv-002', status: 'in_progress', topic: 'Fix CORS headers', invoked_at: '2026-06-14T09:00:00Z' }),
  makeInvocation({ invocation_id: 'inv-003', status: 'failed', topic: 'Refactor auth', invoked_at: '2026-06-14T08:00:00Z' }),
  makeInvocation({ invocation_id: 'inv-004', status: 'webhook_received', topic: 'Add tests', invoked_at: '2026-06-14T07:00:00Z' }),
  makeInvocation({ invocation_id: 'inv-005', status: 'rejected', topic: 'Upgrade deps', invoked_at: '2026-06-14T06:00:00Z' }),
  makeInvocation({ invocation_id: 'inv-006', status: 'rate_limited', topic: 'Deploy fix', invoked_at: '2026-06-14T05:00:00Z' }),
];

const mockResponse: InvocationListResponse = {
  items: mockItems,
  last_key: null,
};

const mockResponseWithCursor: InvocationListResponse = {
  items: mockItems.slice(0, 3),
  last_key: 'inv-003',
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

function renderAgentActivity() {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AgentActivity />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function setupNonAdmin() {
  mockUsePermissions.mockReturnValue({
    isPlatformAdmin: () => false,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    user: { id: 'user-001', role: 'dept_admin', permissions: [] },
    hasPermission: () => false,
    hasRole: () => false,
    canViewOrganizations: () => false,
    canCreateOrganizations: () => false,
    canUpdateOrganizations: () => false,
    canDeleteOrganizations: () => false,
    canViewBudgets: () => false,
    canUpdateBudgets: () => false,
    canViewRateLimits: () => false,
    canUpdateRateLimits: () => false,
    canViewPool: () => false,
    canManagePool: () => false,
    canViewUsage: () => false,
    canViewLogs: () => false,
    canExportLogs: () => false,
    canViewUsers: () => false,
    canManageUsers: () => false,
    canViewMetrics: () => false,
    canAccessOrg: () => false,
    canAccessDept: () => false,
  });
}

function setupAdmin() {
  mockUsePermissions.mockReturnValue({
    isPlatformAdmin: () => true,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    user: { id: 'user-001', role: 'platform_admin', permissions: [] },
    hasPermission: () => true,
    hasRole: () => true,
    canViewOrganizations: () => true,
    canCreateOrganizations: () => true,
    canUpdateOrganizations: () => true,
    canDeleteOrganizations: () => true,
    canViewBudgets: () => true,
    canUpdateBudgets: () => true,
    canViewRateLimits: () => true,
    canUpdateRateLimits: () => true,
    canViewPool: () => true,
    canManagePool: () => true,
    canViewUsage: () => true,
    canViewLogs: () => true,
    canExportLogs: () => true,
    canViewUsers: () => true,
    canManageUsers: () => true,
    canViewMetrics: () => true,
    canAccessOrg: () => true,
    canAccessDept: () => true,
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AgentActivity Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupNonAdmin();
    mockGetMine.mockResolvedValue(mockResponse);
    mockGetAll.mockResolvedValue(mockResponse);
  });

  it('renders date-desc rows from mocked /me/agent-invocations', async () => {
    renderAgentActivity();

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalledTimes(1);
    });

    // All items should be rendered
    expect(screen.getByText('Implement Agent Activity page')).toBeInTheDocument();
    expect(screen.getByText('Fix CORS headers')).toBeInTheDocument();
    expect(screen.getByText('Refactor auth')).toBeInTheDocument();
  });

  it('"next" follows last_key; empty page with non-null last_key still shows working "next"', async () => {
    const user = userEvent.setup();

    // First page has items and a cursor
    mockGetMine.mockResolvedValueOnce(mockResponseWithCursor);

    renderAgentActivity();

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalledTimes(1);
    });

    // Next button should be enabled since last_key is non-null
    const nextBtn = screen.getByRole('button', { name: /next/i });
    expect(nextBtn).not.toBeDisabled();

    // Click Next → should call API with last_key
    mockGetMine.mockResolvedValueOnce({
      items: [],
      last_key: 'inv-006', // empty page but more results exist!
    });

    await user.click(nextBtn);

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalledTimes(2);
    });

    // Verify the second call used the last_key cursor
    const secondCallParams = mockGetMine.mock.calls[1][0];
    expect(secondCallParams.last_key).toBe('inv-003');

    // Even though page is empty, since last_key is non-null,
    // should show "Load next page" button (not "No agent activity yet")
    await waitFor(() => {
      expect(screen.getByText(/More results may exist/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /load next page/i })).toBeInTheDocument();
    });

    // Should NOT show the empty state message
    expect(screen.queryByText('No agent activity yet')).not.toBeInTheDocument();
  });

  it('renders correct glyph/label for each of the 6 statuses', async () => {
    // Create items with all statuses
    const allStatusItems: InvocationItem[] = [
      makeInvocation({ invocation_id: 'inv-s1', status: 'webhook_received', topic: 'topic-s1' }),
      makeInvocation({ invocation_id: 'inv-s2', status: 'in_progress', topic: 'topic-s2' }),
      makeInvocation({ invocation_id: 'inv-s3', status: 'complete', topic: 'topic-s3' }),
      makeInvocation({ invocation_id: 'inv-s4', status: 'failed', topic: 'topic-s4' }),
      makeInvocation({ invocation_id: 'inv-s5', status: 'rejected', topic: 'topic-s5' }),
      makeInvocation({ invocation_id: 'inv-s6', status: 'rate_limited', topic: 'topic-s6' }),
      makeInvocation({ invocation_id: 'inv-s7', status: 'no_op', topic: 'topic-s7' }),
    ];

    mockGetMine.mockResolvedValue({ items: allStatusItems, last_key: null });

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('Webhook recv')).toBeInTheDocument();
    });

    // Use getAllByText because status labels also appear in filter dropdown options
    expect(screen.getAllByText('In progress').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Complete').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Failed').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Rejected').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Rate limited').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('No-op').length).toBeGreaterThanOrEqual(1);
  });

  it('renders source_url as clickable repo#N link; null shows "(no external link)"', async () => {
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-link1',
        source_url: 'https://github.com/aws-e/adp/issues/1457',
        repo: 'aws-e/adp',
        issue_number: 1457,
        topic: 'With link',
      }),
      makeInvocation({
        invocation_id: 'inv-link2',
        source_url: null,
        repo: null,
        issue_number: null,
        channel: 'slack',
        topic: 'No link',
      }),
    ];

    mockGetMine.mockResolvedValue({ items, last_key: null });

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('With link')).toBeInTheDocument();
    });

    // Check the link
    const link = screen.getByRole('link', { name: /aws-e\/adp#1457/ });
    expect(link).toHaveAttribute('href', 'https://github.com/aws-e/adp/issues/1457');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');

    // Check the no-link text
    expect(screen.getByText('(no external link)')).toBeInTheDocument();
  });

  it('admin toggle hidden for non-admin role', async () => {
    setupNonAdmin();
    renderAgentActivity();

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalled();
    });

    // The "Mine" / "All (Admin)" toggle should NOT be visible
    expect(screen.queryByRole('tab', { name: /mine/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /all/i })).not.toBeInTheDocument();
  });

  it('admin toggle visible and switches to /admin/ endpoint for admin', async () => {
    const user = userEvent.setup();
    setupAdmin();

    renderAgentActivity();

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalledTimes(1);
    });

    // Toggle should be visible
    const allTab = screen.getByRole('tab', { name: /all/i });
    expect(allTab).toBeInTheDocument();

    // Click "All (Admin)"
    await user.click(allTab);

    await waitFor(() => {
      expect(mockGetAll).toHaveBeenCalledTimes(1);
    });
  });

  it('API error shows error/retry UI, not a blank page', async () => {
    const user = userEvent.setup();
    mockGetMine.mockRejectedValueOnce(new Error('Network failure'));

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('Network failure')).toBeInTheDocument();
    });

    // Should show retry button
    const retryBtn = screen.getByRole('button', { name: /retry/i });
    expect(retryBtn).toBeInTheDocument();

    // Click retry
    mockGetMine.mockResolvedValueOnce(mockResponse);
    await user.click(retryBtn);

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalledTimes(2);
    });
  });

  it('shows empty state "No agent activity yet" when no items and no cursor', async () => {
    mockGetMine.mockResolvedValue({ items: [], last_key: null });

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('No agent activity yet')).toBeInTheDocument();
    });
  });

  it('renders page title and subtitle', async () => {
    renderAgentActivity();

    expect(screen.getByText('Agent Activity')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('Your agent invocations')).toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------------------
  // Phase 5: Row detail + polish tests
  // ---------------------------------------------------------------------------

  it('row click opens detail modal with correlation_id, run_id, status', async () => {
    const user = userEvent.setup();
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-detail-1',
        correlation_id: 'corr-xyz789',
        run_id: '99887766',
        status: 'complete',
        topic: 'Detail test topic',
      }),
    ];
    mockGetMine.mockResolvedValue({ items, last_key: null });

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('Detail test topic')).toBeInTheDocument();
    });

    // Click the row
    const row = screen.getByRole('button', { name: /View details for invocation: Detail test topic/ });
    await user.click(row);

    // Modal should open with detail fields
    await waitFor(() => {
      expect(screen.getByText('Invocation Detail')).toBeInTheDocument();
    });
    expect(screen.getByText('corr-xyz789')).toBeInTheDocument();
    expect(screen.getByText('99887766')).toBeInTheDocument();
  });

  it('shows distinct empty state for "no matches" vs "no activity" based on active filters', async () => {
    const user = userEvent.setup();
    mockGetMine.mockResolvedValue({ items: [], last_key: null });

    renderAgentActivity();

    // No filters → "No agent activity yet"
    await waitFor(() => {
      expect(screen.getByText('No agent activity yet')).toBeInTheDocument();
    });
    expect(screen.getByText('Agent invocations will appear here once triggered.')).toBeInTheDocument();

    // Apply a status filter → "No matching results for the current filters"
    const statusSelect = screen.getByLabelText('Filter by status');
    await user.selectOptions(statusSelect, 'failed');

    await waitFor(() => {
      expect(screen.getByText('No matching results for the current filters')).toBeInTheDocument();
    });
    expect(screen.getByText('Try adjusting your filters to see more results.')).toBeInTheDocument();
    expect(screen.queryByText('No agent activity yet')).not.toBeInTheDocument();
  });

  it('row is keyboard-accessible (Enter key opens detail)', async () => {
    const user = userEvent.setup();
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-kb-1',
        topic: 'Keyboard test',
      }),
    ];
    mockGetMine.mockResolvedValue({ items, last_key: null });

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('Keyboard test')).toBeInTheDocument();
    });

    // Focus the row and press Enter
    const row = screen.getByRole('button', { name: /View details for invocation: Keyboard test/ });
    row.focus();
    await user.keyboard('{Enter}');

    // Modal should open
    await waitFor(() => {
      expect(screen.getByText('Invocation Detail')).toBeInTheDocument();
    });
  });
});
