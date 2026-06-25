/**
 * Component tests for AssetStatusChips.
 *
 * Issue #1796 (Story G of E10 #1736).
 *
 * Tests:
 * - Loading state
 * - Error state
 * - Repo not found (not yet indexed)
 * - No stages (no stage data)
 * - Full render with chips from fixture
 * - Compact mode renders dots
 * - Failed stages show error toggle
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AssetStatusChips, statusColor } from '@/components/knowledge/AssetStatusChips';

// Mock the knowledge service
vi.mock('@/services/knowledge', () => ({
  getAssetStatus: vi.fn(),
}));

import { getAssetStatus } from '@/services/knowledge';

const mockGetAssetStatus = getAssetStatus as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const fullStatusResponse = {
  assetId: 'asset-001',
  sourceRef: 'https://github.com/acme/my-service',
  repoFound: true,
  runId: 'run-123',
  runStatus: 'completed',
  runStartedAt: '2026-06-20T10:00:00Z',
  stages: [
    {
      stage: 'clone',
      status: 'verified',
      artifactRef: 'sha256:abc',
      error: null,
      startedAt: '2026-06-20T10:01:00Z',
      completedAt: '2026-06-20T10:02:00Z',
    },
    {
      stage: 'cgc_structural',
      status: 'verified',
      artifactRef: null,
      error: null,
      startedAt: '2026-06-20T10:02:00Z',
      completedAt: '2026-06-20T10:03:00Z',
    },
    {
      stage: 'embed_vectors',
      status: 'running',
      artifactRef: null,
      error: null,
      startedAt: '2026-06-20T10:03:00Z',
      completedAt: null,
    },
    {
      stage: 'sbom_source',
      status: 'pending',
      artifactRef: null,
      error: null,
      startedAt: null,
      completedAt: null,
    },
    {
      stage: 'deepwiki',
      status: 'skipped',
      artifactRef: null,
      error: null,
      startedAt: null,
      completedAt: null,
    },
    {
      stage: 'zoekt_index',
      status: 'failed',
      artifactRef: null,
      error: 'Timeout after 300s',
      startedAt: '2026-06-20T10:03:00Z',
      completedAt: '2026-06-20T10:08:00Z',
    },
  ],
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AssetStatusChips', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows loading state while fetching', () => {
    // Never resolve the promise
    mockGetAssetStatus.mockReturnValue(new Promise(() => {}));

    render(<AssetStatusChips assetId="asset-001" />);
    expect(screen.getByTestId('asset-status-loading')).toBeInTheDocument();
  });

  it('shows error state on fetch failure', async () => {
    mockGetAssetStatus.mockRejectedValue(new Error('Network error'));

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-error')).toBeInTheDocument();
    });
    expect(screen.getByText('Network error')).toBeInTheDocument();
  });

  it('shows "Not yet indexed" when repo not found', async () => {
    mockGetAssetStatus.mockResolvedValue({
      assetId: 'asset-001',
      sourceRef: 'https://github.com/acme/new-repo',
      repoFound: false,
      runId: null,
      runStatus: null,
      runStartedAt: null,
      stages: [],
    });

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-no-repo')).toBeInTheDocument();
    });
    expect(screen.getByText('Not yet indexed')).toBeInTheDocument();
  });

  it('shows "No stage data" when repo found but no stages', async () => {
    mockGetAssetStatus.mockResolvedValue({
      assetId: 'asset-001',
      sourceRef: 'https://github.com/acme/my-service',
      repoFound: true,
      runId: 'run-123',
      runStatus: 'completed',
      runStartedAt: '2026-06-20T10:00:00Z',
      stages: [],
    });

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-no-stages')).toBeInTheDocument();
    });
    expect(screen.getByText('No stage data')).toBeInTheDocument();
  });

  it('renders chips for all canonical stages with correct status', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse);

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    // All 6 canonical stages should be rendered as chips
    expect(screen.getByTestId('stage-chip-clone')).toBeInTheDocument();
    expect(screen.getByTestId('stage-chip-cgc_structural')).toBeInTheDocument();
    expect(screen.getByTestId('stage-chip-embed_vectors')).toBeInTheDocument();
    expect(screen.getByTestId('stage-chip-sbom_source')).toBeInTheDocument();
    expect(screen.getByTestId('stage-chip-deepwiki')).toBeInTheDocument();
    expect(screen.getByTestId('stage-chip-zoekt_index')).toBeInTheDocument();

    // Labels should render correctly
    expect(screen.getByText('Clone')).toBeInTheDocument();
    expect(screen.getByText('Structural')).toBeInTheDocument();
    expect(screen.getByText('Vectors')).toBeInTheDocument();
    expect(screen.getByText('SBOM')).toBeInTheDocument();
    expect(screen.getByText('Wiki')).toBeInTheDocument();
    expect(screen.getByText('Zoekt')).toBeInTheDocument();
  });

  it('shows error toggle when stages have failures', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse);

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    // Error toggle should be visible
    const errorToggle = screen.getByTestId('toggle-errors');
    expect(errorToggle).toHaveTextContent('1 error(s)');

    // Click to show errors
    await userEvent.click(errorToggle);
    expect(screen.getByText('Timeout after 300s')).toBeInTheDocument();
  });

  it('renders compact mode with dots', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse);

    render(<AssetStatusChips assetId="asset-001" compact />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips-compact')).toBeInTheDocument();
    });

    // Should render dots, not text labels
    expect(screen.queryByText('Clone')).not.toBeInTheDocument();
  });

  it('compact mode shows "-" when repo not found', async () => {
    mockGetAssetStatus.mockResolvedValue({
      assetId: 'asset-001',
      sourceRef: 'https://github.com/acme/new-repo',
      repoFound: false,
      runId: null,
      runStatus: null,
      runStartedAt: null,
      stages: [],
    });

    render(<AssetStatusChips assetId="asset-001" compact />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-no-repo')).toBeInTheDocument();
    });
    expect(screen.getByText('-')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// statusColor utility tests
// ---------------------------------------------------------------------------

describe('statusColor', () => {
  it('returns green classes for verified status', () => {
    expect(statusColor('verified')).toContain('bg-green');
  });

  it('returns red classes for failed status', () => {
    expect(statusColor('failed')).toContain('bg-red');
  });

  it('returns blue classes for running status', () => {
    expect(statusColor('running')).toContain('bg-blue');
  });

  it('returns yellow classes for skipped status', () => {
    expect(statusColor('skipped')).toContain('bg-yellow');
  });

  it('returns gray classes for unknown/pending status', () => {
    expect(statusColor('pending')).toContain('bg-gray');
    expect(statusColor('unknown')).toContain('bg-gray');
  });
});
