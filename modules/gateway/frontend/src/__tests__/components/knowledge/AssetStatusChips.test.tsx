/**
 * Component tests for AssetStatusChips.
 *
 * Issue #1796 (Story G of E10 #1736).
 * Issue #2309 (Story 3 of EPIC #2292): 4-state rendering, metrics, workerPod.
 * Issue #2310 (Story 5 of EPIC #2292): Live polling, polling indicator, enablePolling prop.
 *
 * Tests:
 * - Loading state
 * - Error state
 * - Repo not found (not yet indexed)
 * - No stages — repo type (no stage data)
 * - No stages — url/doc type (stage tracking not available)
 * - Full render with 4-state chips (verified/failed/skipped/not-available)
 * - Metrics display on verified stages
 * - WorkerPod display on failed stages
 * - Compact mode renders dots
 * - Failed stages show error toggle with workerPod
 * - Polling indicator shown for non-terminal states
 * - No polling indicator when enablePolling=false
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
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
      metrics: { files: 150 },
      workerPod: null,
    },
    {
      stage: 'cgc_structural',
      status: 'verified',
      artifactRef: null,
      error: null,
      startedAt: '2026-06-20T10:02:00Z',
      completedAt: '2026-06-20T10:03:00Z',
      metrics: { symbols: 5000 },
      workerPod: 'worker-abc-123',
    },
    {
      stage: 'embed_vectors',
      status: 'verified',
      artifactRef: null,
      error: null,
      startedAt: '2026-06-20T10:03:00Z',
      completedAt: '2026-06-20T10:04:00Z',
      metrics: { vectors: 12000 },
      workerPod: 'worker-abc-123',
    },
    {
      stage: 'sbom_source',
      status: 'verified',
      artifactRef: null,
      error: null,
      startedAt: '2026-06-20T10:04:00Z',
      completedAt: '2026-06-20T10:05:00Z',
      metrics: { packages: 87 },
      workerPod: 'worker-abc-123',
    },
    {
      stage: 'deepwiki',
      status: 'failed',
      artifactRef: null,
      error: 'Timeout after 300s',
      startedAt: '2026-06-20T10:05:00Z',
      completedAt: '2026-06-20T10:10:00Z',
      metrics: null,
      workerPod: 'worker-wiki-456',
    },
    {
      stage: 'graphrag',
      status: 'skipped',
      artifactRef: null,
      error: null,
      startedAt: null,
      completedAt: null,
      metrics: null,
      workerPod: null,
    },
    // zoekt_index intentionally missing (no row) → should render "not available"
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

  it('shows "No stage data" when repo-type asset has empty stages', async () => {
    mockGetAssetStatus.mockResolvedValue({
      assetId: 'asset-001',
      sourceRef: 'https://github.com/acme/my-service',
      repoFound: true,
      runId: 'run-123',
      runStatus: 'completed',
      runStartedAt: '2026-06-20T10:00:00Z',
      stages: [],
    });

    render(<AssetStatusChips assetId="asset-001" assetType="repo" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-no-stages')).toBeInTheDocument();
    });
    expect(screen.getByText('No stage data')).toBeInTheDocument();
  });

  it('shows "Stage tracking not yet available" for url asset with zero stages', async () => {
    mockGetAssetStatus.mockResolvedValue({
      assetId: 'asset-url-001',
      sourceRef: 'https://docs.example.com',
      repoFound: true,
      runId: null,
      runStatus: null,
      runStartedAt: null,
      stages: [],
    });

    render(<AssetStatusChips assetId="asset-url-001" assetType="url" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-no-stages')).toBeInTheDocument();
    });
    expect(
      screen.getByText('Stage tracking not yet available for this asset type'),
    ).toBeInTheDocument();
  });

  it('shows "Stage tracking not yet available" for doc asset with zero stages', async () => {
    mockGetAssetStatus.mockResolvedValue({
      assetId: 'asset-doc-001',
      sourceRef: 'uploaded-doc.pdf',
      repoFound: true,
      runId: null,
      runStatus: null,
      runStartedAt: null,
      stages: [],
    });

    render(<AssetStatusChips assetId="asset-doc-001" assetType="doc" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-no-stages')).toBeInTheDocument();
    });
    expect(
      screen.getByText('Stage tracking not yet available for this asset type'),
    ).toBeInTheDocument();
  });

  it('renders 4-state chips: verified, failed, skipped, not-available', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse);

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    // Verified stages render with ✓
    const cloneChip = screen.getByTestId('stage-chip-clone');
    expect(cloneChip).toHaveTextContent('✓');
    expect(cloneChip).toHaveTextContent('Clone');

    const codeGraphChip = screen.getByTestId('stage-chip-cgc_structural');
    expect(codeGraphChip).toHaveTextContent('✓');
    expect(codeGraphChip).toHaveTextContent('Code Graph');

    // Failed stage renders with ✗
    const wikiChip = screen.getByTestId('stage-chip-deepwiki');
    expect(wikiChip).toHaveTextContent('✗');
    expect(wikiChip).toHaveTextContent('Wiki');

    // Skipped stage renders with "skipped" label
    const graphragChip = screen.getByTestId('stage-chip-graphrag');
    expect(graphragChip).toHaveTextContent('Knowledge Graph');
    expect(graphragChip).toHaveTextContent('skipped');

    // Not-available stage (zoekt_index has no row) renders with dash
    const zoektChip = screen.getByTestId('stage-chip-zoekt_index');
    expect(zoektChip).toHaveTextContent('Code Search');
    expect(zoektChip).toHaveTextContent('—');
  });

  it('renders friendly labels (canonical names from design)', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse);

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    // Check friendly labels are rendered via their chips (test-ids are stable)
    expect(screen.getByTestId('stage-chip-clone')).toHaveTextContent('Clone');
    expect(screen.getByTestId('stage-chip-cgc_structural')).toHaveTextContent('Code Graph');
    expect(screen.getByTestId('stage-chip-embed_vectors')).toHaveTextContent('Embeddings');
    expect(screen.getByTestId('stage-chip-sbom_source')).toHaveTextContent('Dependencies (SBOM)');
    expect(screen.getByTestId('stage-chip-deepwiki')).toHaveTextContent('Wiki');
    expect(screen.getByTestId('stage-chip-zoekt_index')).toHaveTextContent('Code Search');
    expect(screen.getByTestId('stage-chip-graphrag')).toHaveTextContent('Knowledge Graph');
  });

  it('renders metrics for verified stages', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse);

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    // Clone has metrics: { files: 150 }
    const cloneMetrics = screen.getByTestId('stage-metrics-clone');
    expect(cloneMetrics).toHaveTextContent('150 files');

    // Code Graph has metrics: { symbols: 5000 }
    const cgcMetrics = screen.getByTestId('stage-metrics-cgc_structural');
    expect(cgcMetrics).toHaveTextContent('5,000 symbols');

    // Embeddings has metrics: { vectors: 12000 }
    const embedMetrics = screen.getByTestId('stage-metrics-embed_vectors');
    expect(embedMetrics).toHaveTextContent('12,000 vectors');

    // SBOM has metrics: { packages: 87 }
    const sbomMetrics = screen.getByTestId('stage-metrics-sbom_source');
    expect(sbomMetrics).toHaveTextContent('87 packages');
  });

  it('renders metrics with multiple values (nodes/edges)', async () => {
    mockGetAssetStatus.mockResolvedValue({
      ...fullStatusResponse,
      stages: [
        {
          stage: 'scip_structural',
          status: 'verified',
          artifactRef: null,
          error: null,
          startedAt: '2026-06-20T10:02:00Z',
          completedAt: '2026-06-20T10:03:00Z',
          metrics: { nodes: 12548, edges: 16320 },
          workerPod: null,
        },
      ],
    });

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    const scipMetrics = screen.getByTestId('stage-metrics-scip_structural');
    expect(scipMetrics).toHaveTextContent('12,548 nodes');
    expect(scipMetrics).toHaveTextContent('16,320 edges');
  });

  it('shows workerPod in error details for failed stages', async () => {
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

    // WorkerPod should be shown for log lookup
    const podElement = screen.getByTestId('worker-pod-deepwiki');
    expect(podElement).toHaveTextContent('pod: worker-wiki-456');
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

  it('does not render metrics for stages without metrics', async () => {
    mockGetAssetStatus.mockResolvedValue({
      ...fullStatusResponse,
      stages: [
        {
          stage: 'clone',
          status: 'verified',
          artifactRef: null,
          error: null,
          startedAt: '2026-06-20T10:01:00Z',
          completedAt: '2026-06-20T10:02:00Z',
          metrics: null,
          workerPod: null,
        },
      ],
    });

    render(<AssetStatusChips assetId="asset-001" />);
    await waitFor(() => {
      expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    });

    expect(screen.queryByTestId('stage-metrics-clone')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// statusColor utility tests
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Polling behavior tests (Issue #2310)
// ---------------------------------------------------------------------------

describe('AssetStatusChips — polling behavior', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows polling indicator when status is non-terminal (indexing)', async () => {
    const indexingResponse = {
      ...fullStatusResponse,
      runStatus: 'indexing',
      stages: [
        { stage: 'clone', status: 'verified', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: { files: 50 }, workerPod: null },
        { stage: 'embed_vectors', status: 'running', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: null, workerPod: 'worker-1' },
      ],
    };
    mockGetAssetStatus.mockResolvedValue(indexingResponse);

    render(<AssetStatusChips assetId="asset-001" enablePolling={true} />);

    // Advance timers and flush promises
    await act(async () => { vi.advanceTimersByTime(0); });

    expect(screen.getByTestId('polling-indicator')).toBeInTheDocument();
    expect(screen.getByText('Updating live')).toBeInTheDocument();
  });

  it('does not show polling indicator when status is terminal', async () => {
    mockGetAssetStatus.mockResolvedValue(fullStatusResponse); // runStatus: 'completed'

    render(<AssetStatusChips assetId="asset-001" enablePolling={true} />);

    await act(async () => { vi.advanceTimersByTime(0); });

    expect(screen.getByTestId('asset-status-chips')).toBeInTheDocument();
    expect(screen.queryByTestId('polling-indicator')).not.toBeInTheDocument();
  });

  it('does not show polling indicator when enablePolling is false', async () => {
    const indexingResponse = {
      ...fullStatusResponse,
      runStatus: 'indexing',
      stages: [
        { stage: 'clone', status: 'running', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: null, workerPod: null },
      ],
    };
    mockGetAssetStatus.mockResolvedValue(indexingResponse);

    render(<AssetStatusChips assetId="asset-001" enablePolling={false} />);

    await act(async () => { vi.advanceTimersByTime(0); });

    // With enablePolling=false, the hook doesn't fetch at all, so we get loading/empty state
    expect(screen.queryByTestId('polling-indicator')).not.toBeInTheDocument();
  });

  it('calls onStatusChange when status transitions', async () => {
    const onStatusChange = vi.fn();
    const indexingResponse = {
      ...fullStatusResponse,
      runStatus: 'indexing',
      stages: [
        { stage: 'clone', status: 'running', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: null, workerPod: null },
      ],
    };
    mockGetAssetStatus.mockResolvedValue(indexingResponse);

    render(
      <AssetStatusChips
        assetId="asset-001"
        enablePolling={true}
        onStatusChange={onStatusChange}
      />,
    );

    await act(async () => { vi.advanceTimersByTime(0); });

    // Initial transition: null → indexing
    expect(onStatusChange).toHaveBeenCalledWith(null, 'indexing');
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
