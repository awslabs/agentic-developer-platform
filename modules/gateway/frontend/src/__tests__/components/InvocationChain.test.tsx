/**
 * Tests for the InvocationChain component.
 *
 * Issue #1461: Phase 6 — chain view for agent-to-agent lineage.
 * Validates: tree rendering, flat fallback, root indicator, depth cap notice.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import InvocationChain from '@/components/InvocationChain';
import type { InvocationChainResponse } from '@/types/activity';

// Mock the services
vi.mock('@/services/activity', () => ({
  getMyInvocationChain: vi.fn(),
  getAdminInvocationChain: vi.fn(),
}));

import { getMyInvocationChain, getAdminInvocationChain } from '@/services/activity';

const mockGetMyChain = getMyInvocationChain as ReturnType<typeof vi.fn>;
const mockGetAdminChain = getAdminInvocationChain as ReturnType<typeof vi.fn>;

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

function renderChain(props: Partial<React.ComponentProps<typeof InvocationChain>> = {}) {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <InvocationChain
          correlationId="chain-001"
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const linearChainResponse: InvocationChainResponse = {
  correlation_id: 'chain-001',
  root_human_id: 'user-001',
  is_human_rooted: true,
  items: [
    {
      invocation_id: 'inv-A',
      invoked_at: '2026-06-14T10:00:00Z',
      channel: 'github',
      status: 'complete',
      topic: 'Deploy infrastructure',
      persona: 'ops',
      parent_invocation_id: null,
      children: [
        {
          invocation_id: 'inv-B',
          invoked_at: '2026-06-14T10:05:00Z',
          channel: 'github',
          status: 'in_progress',
          topic: 'Run migrations',
          persona: 'developer',
          parent_invocation_id: 'inv-A',
          children: [
            {
              invocation_id: 'inv-C',
              invoked_at: '2026-06-14T10:10:00Z',
              channel: 'github',
              status: 'failed',
              topic: 'Verify deployment',
              persona: 'reviewer',
              parent_invocation_id: 'inv-B',
              children: [],
            },
          ],
        },
      ],
    },
  ],
  total_count: 3,
  depth_capped: false,
};

const flatChainResponse: InvocationChainResponse = {
  correlation_id: 'chain-flat',
  root_human_id: 'user-001',
  is_human_rooted: true,
  items: [
    {
      invocation_id: 'inv-X',
      invoked_at: '2026-06-14T10:00:00Z',
      channel: 'github',
      status: 'complete',
      topic: 'Task X',
      persona: 'developer',
      parent_invocation_id: null,
      children: [],
    },
    {
      invocation_id: 'inv-Y',
      invoked_at: '2026-06-14T10:05:00Z',
      channel: 'github',
      status: 'complete',
      topic: 'Task Y',
      persona: 'developer',
      parent_invocation_id: null,
      children: [],
    },
  ],
  total_count: 2,
  depth_capped: false,
};

const botRootedResponse: InvocationChainResponse = {
  correlation_id: 'chain-bot',
  root_human_id: null,
  is_human_rooted: false,
  items: [
    {
      invocation_id: 'inv-bot-1',
      invoked_at: '2026-06-14T00:00:00Z',
      channel: 'api',
      status: 'complete',
      topic: 'Scheduled cleanup',
      persona: 'ops',
      parent_invocation_id: null,
      children: [],
    },
  ],
  total_count: 1,
  depth_capped: false,
};

const depthCappedResponse: InvocationChainResponse = {
  correlation_id: 'chain-deep',
  root_human_id: 'user-001',
  is_human_rooted: true,
  items: [
    {
      invocation_id: 'inv-1',
      invoked_at: '2026-06-14T10:00:00Z',
      channel: 'github',
      status: 'complete',
      topic: 'Deep root',
      persona: null,
      parent_invocation_id: null,
      children: [],
    },
  ],
  total_count: 50,
  depth_capped: true,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InvocationChain Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetMyChain.mockResolvedValue(linearChainResponse);
    mockGetAdminChain.mockResolvedValue(linearChainResponse);
  });

  it('renders A→B→C indented tree by parent_invocation_id', async () => {
    renderChain();

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    // All three nodes should be visible
    expect(screen.getByText('Deploy infrastructure')).toBeInTheDocument();
    expect(screen.getByText('Run migrations')).toBeInTheDocument();
    expect(screen.getByText('Verify deployment')).toBeInTheDocument();

    // Check tree structure via test IDs
    expect(screen.getByTestId('chain-node-inv-A')).toBeInTheDocument();
    expect(screen.getByTestId('chain-node-inv-B')).toBeInTheDocument();
    expect(screen.getByTestId('chain-node-inv-C')).toBeInTheDocument();
  });

  it('shows root human indicator for human-rooted chains', async () => {
    renderChain();

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    // Should show "Started by you"
    expect(screen.getByText(/Started by/)).toBeInTheDocument();
    expect(screen.getByText('you')).toBeInTheDocument();
  });

  it('shows "Agent-initiated" for bot-rooted chains', async () => {
    mockGetMyChain.mockResolvedValue(botRootedResponse);

    renderChain({ correlationId: 'chain-bot' });

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    expect(screen.getByText(/Agent-initiated/)).toBeInTheDocument();
  });

  it('renders flat list when no parent edges exist (pre-feature rows)', async () => {
    mockGetMyChain.mockResolvedValue(flatChainResponse);

    renderChain({ correlationId: 'chain-flat' });

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    // Both items rendered at root level (no connector arrows except at depth > 0)
    expect(screen.getByText('Task X')).toBeInTheDocument();
    expect(screen.getByText('Task Y')).toBeInTheDocument();
    expect(screen.getByTestId('chain-node-inv-X')).toBeInTheDocument();
    expect(screen.getByTestId('chain-node-inv-Y')).toBeInTheDocument();
  });

  it('shows depth cap notice when chain is truncated', async () => {
    mockGetMyChain.mockResolvedValue(depthCappedResponse);

    renderChain({ correlationId: 'chain-deep' });

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    expect(screen.getByText(/truncated at depth cap/)).toBeInTheDocument();
    expect(screen.getByText('50 invocations')).toBeInTheDocument();
  });

  it('shows total count of invocations', async () => {
    renderChain();

    await waitFor(() => {
      expect(screen.getByText('3 invocations')).toBeInTheDocument();
    });
  });

  it('shows empty state when no chain data', async () => {
    mockGetMyChain.mockResolvedValue({
      correlation_id: 'chain-empty',
      root_human_id: null,
      is_human_rooted: true,
      items: [],
      total_count: 0,
      depth_capped: false,
    });

    renderChain({ correlationId: 'chain-empty' });

    await waitFor(() => {
      expect(screen.getByText('No chain data available')).toBeInTheDocument();
    });
  });

  it('shows error state and retry button on failure', async () => {
    mockGetMyChain.mockRejectedValue(new Error('Network failure'));

    renderChain();

    await waitFor(() => {
      expect(screen.getByText('Network failure')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('highlights the specified invocation', async () => {
    renderChain({ highlightInvocationId: 'inv-B' });

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    // The highlighted node should have a distinguishing style
    const highlightedNode = screen.getByTestId('chain-node-inv-B');
    expect(highlightedNode).toBeInTheDocument();
  });

  it('calls onClose when close button is clicked', async () => {
    const onClose = vi.fn();
    renderChain({ onClose });

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    const closeBtn = screen.getByRole('button', { name: /close/i });
    closeBtn.click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('uses admin endpoint when isAdmin is true', async () => {
    renderChain({ isAdmin: true, tenantId: 'org-xyz' });

    await waitFor(() => {
      expect(mockGetAdminChain).toHaveBeenCalledWith('chain-001', 'org-xyz', false);
    });
  });

  it('shows persona badges on chain nodes', async () => {
    renderChain();

    await waitFor(() => {
      expect(screen.getByTestId('invocation-chain')).toBeInTheDocument();
    });

    expect(screen.getByText('ops')).toBeInTheDocument();
    expect(screen.getByText('developer')).toBeInTheDocument();
    expect(screen.getByText('reviewer')).toBeInTheDocument();
  });
});
