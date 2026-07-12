/**
 * Tests for the AgentActivity page.
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Issue #1461: Phase 6 — Trigger badge + chain view.
 * Validates: pagination with cursor/last_key, status rendering, link column,
 * admin toggle visibility, error/retry UI, trigger badges, chain view.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import AgentActivity from '@/pages/AgentActivity';
import type { InvocationItem, InvocationListResponse } from '@/types/activity';

// Mock the services
vi.mock('@/services/activity', () => ({
  getMyInvocations: vi.fn(),
  getMyChains: vi.fn(),
  getAllInvocations: vi.fn(),
  getMyInvocationChain: vi.fn(),
  getAdminInvocationChain: vi.fn(),
}));

// Mock the permissions hook
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: vi.fn(),
}));

import { getMyInvocations, getMyChains, getAllInvocations, getMyInvocationChain } from '@/services/activity';
import { usePermissions } from '@/hooks/usePermissions';

const mockGetMine = getMyInvocations as ReturnType<typeof vi.fn>;
const mockGetMyChains = getMyChains as ReturnType<typeof vi.fn>;
const mockGetAll = getAllInvocations as ReturnType<typeof vi.fn>;
const mockGetMyChain = getMyInvocationChain as ReturnType<typeof vi.fn>;
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
    // Phase 6 lineage defaults
    trigger_kind: 'human',
    triggered_by_invocation_id: null,
    triggered_by_topic: null,
    root_human_id: 'user-001',
    is_human_rooted: true,
    correlation_id: 'chain-001',
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

function renderAgentActivity(initialRoute = '/') {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialRoute]}>
        <AgentActivity />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * Renders the AgentActivity page and switches to "By run" (flat table) mode.
 *
 * The component defaults to "By chain" view (issue #1662), which disables the
 * flat query (getMyInvocations). Tests that assert on flat-table rows, status
 * badges, trigger columns, etc. need the "By run" view active.
 */
async function renderAgentActivityFlat() {
  const user = userEvent.setup();
  const result = renderAgentActivity();

  // Wait for the default chain view to load
  await waitFor(() => {
    expect(mockGetMyChains).toHaveBeenCalled();
  });

  // Switch to "By run" (flat) mode
  const byRunTab = screen.getByRole('tab', { name: /by run/i });
  await user.click(byRunTab);

  // Wait for the flat query to fire
  await waitFor(() => {
    expect(mockGetMine).toHaveBeenCalled();
  });

  return result;
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
    mockGetMyChains.mockResolvedValue({ chains: [], count: 0, last_key: null });
    mockGetAll.mockResolvedValue(mockResponse);
    mockGetMyChain.mockResolvedValue({
      correlation_id: 'chain-001',
      root_human_id: 'user-001',
      is_human_rooted: true,
      items: [],
      total_count: 0,
      depth_capped: false,
    });
  });

  it('renders date-desc rows from mocked /me/agent-invocations', async () => {
    await renderAgentActivityFlat();

    // All items should be rendered in the flat table
    expect(screen.getByText('Implement Agent Activity page')).toBeInTheDocument();
    expect(screen.getByText('Fix CORS headers')).toBeInTheDocument();
    expect(screen.getByText('Refactor auth')).toBeInTheDocument();
  });

  it('"next" follows last_key; empty page with non-null last_key still shows working "next"', async () => {
    const user = userEvent.setup();

    // Override the flat query response: first page has items and a cursor
    mockGetMine.mockResolvedValueOnce(mockResponseWithCursor);

    await renderAgentActivityFlat();

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

    await renderAgentActivityFlat();

    // Scope to the results table — the status filter <select> also renders these
    // labels as <option>s, so a document-wide getByText would match >1 element.
    const table = within(screen.getByRole('table'));
    expect(table.getByText('Webhook recv')).toBeInTheDocument();
    expect(table.getByText('In progress')).toBeInTheDocument();
    expect(table.getByText('Complete')).toBeInTheDocument();
    expect(table.getByText('Failed')).toBeInTheDocument();
    expect(table.getByText('Rejected')).toBeInTheDocument();
    expect(table.getByText('Rate limited')).toBeInTheDocument();
    expect(table.getByText('No-op')).toBeInTheDocument();
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

    await renderAgentActivityFlat();

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
    await renderAgentActivityFlat();

    // The "Mine" / "All (Admin)" toggle should NOT be visible
    expect(screen.queryByRole('tab', { name: /^mine$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /all \(admin\)/i })).not.toBeInTheDocument();
  });

  it('admin toggle visible and switches to /admin/ endpoint for admin', async () => {
    const user = userEvent.setup();
    setupAdmin();

    await renderAgentActivityFlat();

    // Toggle should be visible
    const allTab = screen.getByRole('tab', { name: /all \(admin\)/i });
    expect(allTab).toBeInTheDocument();

    // Click "All (Admin)"
    await user.click(allTab);

    await waitFor(() => {
      expect(mockGetAll).toHaveBeenCalledTimes(1);
    });
  });

  it('API error shows error/retry UI, not a blank page', async () => {
    const user = userEvent.setup();
    // Make the chain query fail (default view is chain mode)
    mockGetMyChains.mockRejectedValueOnce(new Error('Network failure'));

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('Network failure')).toBeInTheDocument();
    });

    // Should show retry button
    const retryBtn = screen.getByRole('button', { name: /retry/i });
    expect(retryBtn).toBeInTheDocument();

    // Click retry
    mockGetMyChains.mockResolvedValueOnce({ chains: [], count: 0, last_key: null });
    await user.click(retryBtn);

    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalledTimes(2);
    });
  });

  it('shows empty state with guidance when no items and no cursor', async () => {
    // In chain view (default), empty means no chains
    mockGetMyChains.mockResolvedValue({ chains: [], count: 0, last_key: null });

    renderAgentActivity();

    await waitFor(() => {
      expect(screen.getByText('No agent runs yet')).toBeInTheDocument();
    });

    // Provides concrete guidance instead of a dead end
    expect(screen.getByText(/Mention the developer agent/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /View setup guide/ })).toHaveAttribute('href', '/setup');
  });

  it('renders page title with cross-link subtitle to Dashboard', async () => {
    renderAgentActivity();

    expect(screen.getByText('Agent Activity')).toBeInTheDocument();

    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });

    expect(screen.getByText(/Every run, filterable/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/runs');
  });

  // ---------------------------------------------------------------------------
  // Phase 6 trigger badge tests (#1461)
  // ---------------------------------------------------------------------------

  it('shows "Started by you" badge for human-triggered invocations', async () => {
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-human',
        trigger_kind: 'human',
        topic: 'Human task',
      }),
    ];

    mockGetMine.mockResolvedValue({ items, last_key: null });

    await renderAgentActivityFlat();

    expect(screen.getByTestId('trigger-badge-human')).toBeInTheDocument();
    expect(screen.getByText('Started by you')).toBeInTheDocument();
  });

  it('shows "Agent-triggered" badge with parent topic for agent-triggered invocations', async () => {
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-agent',
        trigger_kind: 'agent',
        triggered_by_invocation_id: 'inv-parent',
        triggered_by_topic: 'Deploy infrastructure',
        topic: 'Agent child task',
      }),
    ];

    mockGetMine.mockResolvedValue({ items, last_key: null });

    await renderAgentActivityFlat();

    expect(screen.getByTestId('trigger-badge-agent')).toBeInTheDocument();
    expect(screen.getByText('Agent-triggered')).toBeInTheDocument();
    // Shows parent topic
    expect(screen.getByText(/Deploy infrastructure/)).toBeInTheDocument();
  });

  it('shows "Agent-initiated" badge for bot-rooted invocations', async () => {
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-bot',
        trigger_kind: 'bot',
        is_human_rooted: false,
        root_human_id: null,
        topic: 'Cron job',
      }),
    ];

    mockGetMine.mockResolvedValue({ items, last_key: null });

    await renderAgentActivityFlat();

    expect(screen.getByTestId('trigger-badge-bot')).toBeInTheDocument();
    expect(screen.getByText('Agent-initiated')).toBeInTheDocument();
  });

  it('shows "View chain" link when correlation_id is present', async () => {
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-with-chain',
        correlation_id: 'chain-abc',
        topic: 'Chained task',
      }),
    ];

    mockGetMine.mockResolvedValue({ items, last_key: null });

    await renderAgentActivityFlat();

    expect(screen.getByText('View chain')).toBeInTheDocument();
  });

  it('Trigger column header is present in table', async () => {
    await renderAgentActivityFlat();

    expect(screen.getByText('Trigger')).toBeInTheDocument();
  });

  it('flat list still works when lineage fields absent (pre-feature rows)', async () => {
    // Simulate pre-feature data that doesn't have lineage fields
    const items: InvocationItem[] = [
      makeInvocation({
        invocation_id: 'inv-old',
        trigger_kind: 'human',
        triggered_by_invocation_id: null,
        triggered_by_topic: null,
        correlation_id: null,
        topic: 'Old style invocation',
      }),
    ];

    mockGetMine.mockResolvedValue({ items, last_key: null });

    await renderAgentActivityFlat();

    expect(screen.getByText('Old style invocation')).toBeInTheDocument();
    // Should show "Started by you" as default
    expect(screen.getByTestId('trigger-badge-human')).toBeInTheDocument();
    // No "View chain" link when correlation_id is null
    expect(screen.queryByText('View chain')).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Issue #3723: include_non_triggering derivation
  // ---------------------------------------------------------------------------

  it('status=in_progress from URL lands on flat view without include_non_triggering (Issue #3723)', async () => {
    // Render with URL param ?status=in_progress (dashboard tile click).
    // A status filter opens the FLAT view: chain grouping filters by root
    // status, so counts would disagree with the tile that was clicked.
    const queryClient = createTestQueryClient();
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/activity?status=in_progress']}>
          <AgentActivity />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalled();
    });
    expect(mockGetMyChains).not.toHaveBeenCalled();

    const flatCallParams = mockGetMine.mock.calls[0][0];
    expect(flatCallParams.status).toBe('in_progress');
    expect(flatCallParams.include_non_triggering).toBeUndefined();

    unmount();
  });

  it('status=no_op lands on flat view WITH include_non_triggering (Issue #3723)', async () => {
    // Render with URL param ?status=no_op (user explicitly filtering by no_op)
    const queryClient = createTestQueryClient();
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/activity?status=no_op']}>
          <AgentActivity />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalled();
    });

    const flatCallParams = mockGetMine.mock.calls[0][0];
    expect(flatCallParams.status).toBe('no_op');
    expect(flatCallParams.include_non_triggering).toBe(true);

    unmount();
  });

  it('no URL params defaults to chain view (Issue #3723 follow-up)', async () => {
    const queryClient = createTestQueryClient();
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/activity']}>
          <AgentActivity />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });

    unmount();
  });

  it('view=runs URL param lands on flat view (Issue #3723 follow-up)', async () => {
    const queryClient = createTestQueryClient();
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/activity?view=runs']}>
          <AgentActivity />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(mockGetMine).toHaveBeenCalled();
    });
    expect(mockGetMyChains).not.toHaveBeenCalled();

    unmount();
  });
});
