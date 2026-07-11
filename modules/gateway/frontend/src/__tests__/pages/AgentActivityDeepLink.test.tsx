/**
 * Tests for Agent Activity deep-link query-param support.
 *
 * Issue #3632: URL-based deep-linking to /activity page.
 * Validates: ?id= auto-opens detail modal, ?status= pre-fills filter,
 * ?since=today pre-fills date, no params = unchanged behavior, combined params.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import AgentActivity from '@/pages/AgentActivity';
import type { InvocationItem } from '@/types/activity';

// Mock the services
vi.mock('@/services/activity', () => ({
  getMyInvocations: vi.fn(),
  getMyChains: vi.fn(),
  getAllInvocations: vi.fn(),
  getMyInvocationChain: vi.fn(),
  getAdminInvocationChain: vi.fn(),
  getMyInvocationDetail: vi.fn(),
}));

// Mock the permissions hook
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: vi.fn(),
}));

import {
  getMyInvocations,
  getMyChains,
  getMyInvocationDetail,
} from '@/services/activity';
import { usePermissions } from '@/hooks/usePermissions';

const mockGetMine = getMyInvocations as ReturnType<typeof vi.fn>;
const mockGetMyChains = getMyChains as ReturnType<typeof vi.fn>;
const mockGetMyDetail = getMyInvocationDetail as ReturnType<typeof vi.fn>;
const mockUsePermissions = usePermissions as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

function makeInvocation(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'inv-deep-001',
    user_id: 'user-001',
    persona: 'developer',
    channel: 'github',
    status: 'failed',
    topic: 'Fix authentication bug',
    summary: 'Attempted to fix OAuth flow',
    source_url: 'https://github.com/aws-e/adp/issues/3000',
    repo: 'aws-e/adp',
    issue_number: 3000,
    invoked_at: '2026-07-10T14:00:00Z',
    completed_at: '2026-07-10T14:30:00Z',
    status_updated_at: '2026-07-10T14:30:00Z',
    run_id: 'run-001',
    trigger_kind: 'human',
    triggered_by_invocation_id: null,
    triggered_by_topic: null,
    root_human_id: 'user-001',
    is_human_rooted: true,
    correlation_id: 'chain-deep-001',
    total_cost_usd: 0.42,
    total_tokens: 12000,
    call_count: 5,
    error_message: 'Token expired during execution',
    run_log_url: null,
    transcript_key: null,
    ...overrides,
  };
}

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

/**
 * Renders AgentActivity with the given URL search params.
 */
function renderWithParams(params: string) {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/activity${params}`]}>
        <AgentActivity />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AgentActivity Deep Link (Issue #3632)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupNonAdmin();
    mockGetMine.mockResolvedValue({ items: [], last_key: null });
    mockGetMyChains.mockResolvedValue({ chains: [], count: 0, last_key: null });
    mockGetMyDetail.mockResolvedValue(makeInvocation());
  });

  it('?id=valid_id fetches detail and auto-opens the detail modal', async () => {
    const detailItem = makeInvocation({ invocation_id: 'inv-deep-001', topic: 'Fix authentication bug' });
    mockGetMyDetail.mockResolvedValue(detailItem);

    renderWithParams('?id=inv-deep-001');

    // Should call getMyInvocationDetail with the id
    await waitFor(() => {
      expect(mockGetMyDetail).toHaveBeenCalledWith('inv-deep-001');
    });

    // The detail modal should render with the invocation's topic
    await waitFor(() => {
      expect(screen.getByText('Fix authentication bug')).toBeInTheDocument();
    });
  });

  it('?id=nonexistent loads page normally with no modal and no error', async () => {
    // Simulate 404 — getMyInvocationDetail rejects
    mockGetMyDetail.mockRejectedValue(new Error('Not found'));

    renderWithParams('?id=nonexistent-id');

    // Should attempt the fetch
    await waitFor(() => {
      expect(mockGetMyDetail).toHaveBeenCalledWith('nonexistent-id');
    });

    // Page should load normally — chain view fires
    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });

    // No error alert should be shown (the deep-link error is silent)
    expect(screen.queryByText('Failed to load agent activity')).not.toBeInTheDocument();

    // Empty state is fine since we have no chains
    expect(screen.getByText('No agent activity yet')).toBeInTheDocument();
  });

  it('?status=failed pre-fills the status filter dropdown', async () => {
    renderWithParams('?status=failed');

    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });

    // The status filter select should have "failed" selected
    const statusSelect = screen.getByLabelText('Filter by status') as HTMLSelectElement;
    expect(statusSelect.value).toBe('failed');
  });

  it('?since=today pre-fills the start date with today', async () => {
    renderWithParams('?since=today');

    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });

    // The start date input should have today's date
    const today = new Date().toISOString().split('T')[0];
    // The Input component renders type="date" without an explicit label association,
    // so query by the input's displayed value instead.
    const dateInputs = screen.getAllByDisplayValue(today);
    expect(dateInputs.length).toBeGreaterThanOrEqual(1);
    expect(dateInputs[0]).toHaveAttribute('type', 'date');
  });

  it('no params loads page unchanged (regression check)', async () => {
    renderWithParams('');

    // Should NOT call getMyInvocationDetail
    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });
    expect(mockGetMyDetail).not.toHaveBeenCalled();

    // Filters should be at defaults
    const statusSelect = screen.getByLabelText('Filter by status') as HTMLSelectElement;
    expect(statusSelect.value).toBe('');

    // Empty state shows normally
    expect(screen.getByText('No agent activity yet')).toBeInTheDocument();
  });

  it('combined ?id=X&status=failed opens modal AND sets filter', async () => {
    const detailItem = makeInvocation({ invocation_id: 'inv-combo', topic: 'Combined deep link test' });
    mockGetMyDetail.mockResolvedValue(detailItem);

    renderWithParams('?id=inv-combo&status=failed');

    // Should fetch detail
    await waitFor(() => {
      expect(mockGetMyDetail).toHaveBeenCalledWith('inv-combo');
    });

    // Modal should open with the item's topic
    await waitFor(() => {
      expect(screen.getByText('Combined deep link test')).toBeInTheDocument();
    });

    // Status filter should be pre-filled
    const statusSelect = screen.getByLabelText('Filter by status') as HTMLSelectElement;
    expect(statusSelect.value).toBe('failed');
  });

  it('?status=invalid_value does not set the filter (graceful handling)', async () => {
    renderWithParams('?status=bogus_value');

    await waitFor(() => {
      expect(mockGetMyChains).toHaveBeenCalled();
    });

    // The status filter should remain at the default (empty = "All statuses")
    const statusSelect = screen.getByLabelText('Filter by status') as HTMLSelectElement;
    expect(statusSelect.value).toBe('');
  });
});
