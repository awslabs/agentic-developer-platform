/**
 * Tests for usePollingStatus hook.
 *
 * Issue #2310 (Story 5 of EPIC #2292): Live updates for the detailed
 * ingestion view — adaptive polling with terminal-state detection.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { usePollingStatus } from '@/hooks/usePollingStatus';

// Mock the knowledge service
vi.mock('@/services/knowledge', () => ({
  getAssetStatus: vi.fn(),
}));

import { getAssetStatus } from '@/services/knowledge';

const mockGetAssetStatus = getAssetStatus as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const indexingStatus = {
  assetId: 'asset-001',
  sourceRef: 'https://github.com/acme/repo',
  repoFound: true,
  runId: 'run-1',
  runStatus: 'indexing',
  runStartedAt: '2026-06-29T10:00:00Z',
  stages: [
    { stage: 'clone', status: 'verified', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: { files: 50 }, workerPod: null },
    { stage: 'embed_vectors', status: 'running', artifactRef: null, error: null, startedAt: '2026-06-29T10:01:00Z', completedAt: null, metrics: null, workerPod: 'worker-1' },
  ],
};

const completedStatus = {
  assetId: 'asset-001',
  sourceRef: 'https://github.com/acme/repo',
  repoFound: true,
  runId: 'run-1',
  runStatus: 'indexed',
  runStartedAt: '2026-06-29T10:00:00Z',
  stages: [
    { stage: 'clone', status: 'verified', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: { files: 50 }, workerPod: null },
    { stage: 'embed_vectors', status: 'verified', artifactRef: null, error: null, startedAt: null, completedAt: null, metrics: { vectors: 1200 }, workerPod: 'worker-1' },
  ],
};

const queuedStatus = {
  assetId: 'asset-001',
  sourceRef: 'https://github.com/acme/repo',
  repoFound: true,
  runId: 'run-1',
  runStatus: 'queued',
  runStartedAt: null,
  stages: [],
};

const failedStatus = {
  ...completedStatus,
  runStatus: 'failed',
  stages: [
    { stage: 'clone', status: 'failed', artifactRef: null, error: 'OOM', startedAt: null, completedAt: null, metrics: null, workerPod: 'worker-1' },
  ],
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('usePollingStatus', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('fetches status on mount and returns data', async () => {
    mockGetAssetStatus.mockResolvedValue(completedStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    expect(result.current.isLoading).toBe(true);

    // Flush the microtask queue (promise resolution)
    await act(async () => {});

    expect(result.current.isLoading).toBe(false);
    expect(result.current.status).toEqual(completedStatus);
    expect(result.current.error).toBeNull();
  });

  it('does not poll when status is terminal (indexed)', async () => {
    mockGetAssetStatus.mockResolvedValue(completedStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.isPolling).toBe(false);
    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);

    // Advance time — should NOT trigger another fetch
    act(() => { vi.advanceTimersByTime(30_000); });
    await act(async () => {});

    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);
  });

  it('does not poll when status is terminal (failed)', async () => {
    mockGetAssetStatus.mockResolvedValue(failedStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.isPolling).toBe(false);
    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);
  });

  it('polls at 5s interval when status is indexing', async () => {
    mockGetAssetStatus.mockResolvedValue(indexingStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.isPolling).toBe(true);
    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);

    // Advance 5s — should trigger a poll
    act(() => { vi.advanceTimersByTime(5_000); });
    await act(async () => {});

    expect(mockGetAssetStatus).toHaveBeenCalledTimes(2);
  });

  it('polls at 15s interval when status is queued', async () => {
    mockGetAssetStatus.mockResolvedValue(queuedStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.isPolling).toBe(true);

    // At 5s — should NOT trigger
    act(() => { vi.advanceTimersByTime(5_000); });
    await act(async () => {});
    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);

    // At 15s total — should trigger
    act(() => { vi.advanceTimersByTime(10_000); });
    await act(async () => {});
    expect(mockGetAssetStatus).toHaveBeenCalledTimes(2);
  });

  it('stops polling when status transitions to terminal', async () => {
    // First call returns indexing, second returns indexed
    mockGetAssetStatus
      .mockResolvedValueOnce(indexingStatus)
      .mockResolvedValueOnce(completedStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.isPolling).toBe(true);

    // Advance to next poll (5s for indexing)
    act(() => { vi.advanceTimersByTime(5_000); });
    await act(async () => {});

    // Should have fetched twice and stopped
    expect(mockGetAssetStatus).toHaveBeenCalledTimes(2);
    expect(result.current.isPolling).toBe(false);
    expect(result.current.status).toEqual(completedStatus);
  });

  it('calls onStatusChange when run status transitions', async () => {
    const onStatusChange = vi.fn();
    mockGetAssetStatus
      .mockResolvedValueOnce(indexingStatus)
      .mockResolvedValueOnce(completedStatus);

    renderHook(() =>
      usePollingStatus({ assetId: 'asset-001', onStatusChange }),
    );

    await act(async () => {});

    // Initial transition: null → indexing
    expect(onStatusChange).toHaveBeenCalledWith(null, 'indexing');

    // Advance to next poll
    act(() => { vi.advanceTimersByTime(5_000); });
    await act(async () => {});

    // Transition: indexing → indexed
    expect(onStatusChange).toHaveBeenCalledWith('indexing', 'indexed');
    expect(onStatusChange).toHaveBeenCalledTimes(2);
  });

  it('does not start polling when enabled=false', async () => {
    mockGetAssetStatus.mockResolvedValue(indexingStatus);

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001', enabled: false }),
    );

    await act(async () => {});

    expect(result.current.isLoading).toBe(false);
    expect(result.current.isPolling).toBe(false);
    expect(mockGetAssetStatus).not.toHaveBeenCalled();
  });

  it('handles fetch errors gracefully on initial load', async () => {
    mockGetAssetStatus.mockRejectedValue(new Error('Network timeout'));

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBe('Network timeout');
    expect(result.current.status).toBeNull();
  });

  it('silently ignores poll errors (does not overwrite error state)', async () => {
    mockGetAssetStatus
      .mockResolvedValueOnce(indexingStatus) // initial: success
      .mockRejectedValueOnce(new Error('Network blip')); // poll: fail

    const { result } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(result.current.error).toBeNull();
    expect(result.current.status).toEqual(indexingStatus);

    // Advance to poll — it fails
    act(() => { vi.advanceTimersByTime(5_000); });
    await act(async () => {});

    // Error should not be set (silent poll failure)
    expect(result.current.error).toBeNull();
    // Status should remain from last successful fetch
    expect(result.current.status).toEqual(indexingStatus);
    // Still polling
    expect(result.current.isPolling).toBe(true);
  });

  it('resets state when assetId changes', async () => {
    mockGetAssetStatus.mockResolvedValue(indexingStatus);

    const { result, rerender } = renderHook(
      ({ assetId }) => usePollingStatus({ assetId }),
      { initialProps: { assetId: 'asset-001' } },
    );

    await act(async () => {});

    expect(result.current.status).toEqual(indexingStatus);

    // Change asset
    mockGetAssetStatus.mockResolvedValue(completedStatus);
    rerender({ assetId: 'asset-002' });

    // Should reset to loading
    expect(result.current.isLoading).toBe(true);

    await act(async () => {});

    expect(result.current.status).toEqual(completedStatus);
    expect(result.current.isPolling).toBe(false);
  });

  it('cleans up interval on unmount', async () => {
    mockGetAssetStatus.mockResolvedValue(indexingStatus);

    const { unmount } = renderHook(() =>
      usePollingStatus({ assetId: 'asset-001' }),
    );

    await act(async () => {});

    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);

    unmount();

    // Advance time — should NOT trigger another fetch
    act(() => { vi.advanceTimersByTime(30_000); });
    await act(async () => {});

    expect(mockGetAssetStatus).toHaveBeenCalledTimes(1);
  });
});
