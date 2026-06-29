/**
 * usePollingStatus — polls asset ingestion status until a terminal state.
 *
 * Issue #2310 (Story 5 of EPIC #2292): Live updates for the detailed
 * ingestion view. Polls GET /assets/{id}/status at adaptive intervals,
 * stopping when the run reaches a terminal state (indexed, failed, removed).
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { getAssetStatus } from '@/services/knowledge';
import type { AssetStatusResponse } from '@/types';

/** Terminal run statuses — polling stops when one of these is reached. */
const TERMINAL_STATUSES = new Set(['indexed', 'failed', 'removed', 'completed']);

/** Polling interval based on current asset/run status. */
function getPollingInterval(runStatus: string | null): number {
  switch (runStatus) {
    case 'indexing':
      return 5_000; // 5s — active progress, fast feedback
    case 'queued':
    case 'registered':
      return 15_000; // 15s — waiting for pickup
    default:
      return 10_000; // 10s — fallback for unknown non-terminal states
  }
}

export interface UsePollingStatusOptions {
  /** Asset ID to poll. Polling restarts when this changes. */
  assetId: string;
  /** Whether polling is enabled. Defaults to true. */
  enabled?: boolean;
  /** Called when the run status transitions (oldStatus, newStatus). */
  onStatusChange?: (oldStatus: string | null, newStatus: string | null) => void;
}

export interface UsePollingStatusResult {
  status: AssetStatusResponse | null;
  isLoading: boolean;
  error: string | null;
  /** Whether the hook is actively polling (non-terminal state). */
  isPolling: boolean;
}

/**
 * Hook that polls asset ingestion status at adaptive intervals.
 * Stops automatically once the run status reaches a terminal state.
 */
export function usePollingStatus({
  assetId,
  enabled = true,
  onStatusChange,
}: UsePollingStatusOptions): UsePollingStatusResult {
  const [status, setStatus] = useState<AssetStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isPolling, setIsPolling] = useState(false);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastRunStatusRef = useRef<string | null>(null);
  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    setIsPolling(false);
  }, []);

  const fetchStatus = useCallback(
    async (isInitial: boolean) => {
      if (isInitial) {
        setIsLoading(true);
        setError(null);
      }
      try {
        const result = await getAssetStatus(assetId);
        setStatus(result);
        setError(null);

        // Detect status transitions
        const newRunStatus = result.runStatus;
        if (lastRunStatusRef.current !== newRunStatus) {
          onStatusChangeRef.current?.(lastRunStatusRef.current, newRunStatus);
          lastRunStatusRef.current = newRunStatus;
        }

        // Stop polling if terminal
        if (newRunStatus && TERMINAL_STATUSES.has(newRunStatus)) {
          stopPolling();
        }

        return result;
      } catch (err) {
        if (isInitial) {
          setError(err instanceof Error ? err.message : 'Failed to load status');
        }
        // On poll errors, silently continue — don't break the interval
        return null;
      } finally {
        if (isInitial) {
          setIsLoading(false);
        }
      }
    },
    [assetId, stopPolling],
  );

  useEffect(() => {
    // Reset state on asset change
    setStatus(null);
    setIsLoading(true);
    setError(null);
    lastRunStatusRef.current = null;
    stopPolling();

    if (!enabled) {
      setIsLoading(false);
      return;
    }

    let cancelled = false;

    async function init() {
      const result = await fetchStatus(true);
      if (cancelled) return;

      // Start polling only if not terminal
      const runStatus = result?.runStatus ?? null;
      if (!runStatus || !TERMINAL_STATUSES.has(runStatus)) {
        const interval = getPollingInterval(runStatus);
        setIsPolling(true);
        intervalRef.current = setInterval(() => {
          if (!cancelled) fetchStatus(false);
        }, interval);
      }
    }

    init();

    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [assetId, enabled, fetchStatus, stopPolling]);

  return { status, isLoading, error, isPolling };
}
