import { useState, useEffect, useCallback, useRef } from 'react';
import { getAccessStatus, type AccessStatus, type AccessStatusResponse } from '@/services/onboarding';

const CACHE_KEY = 'adp_access_status';
const CACHE_TTL_MS = 60_000; // 60 seconds

interface CachedStatus {
  data: AccessStatusResponse;
  timestamp: number;
}

function getCachedStatus(): AccessStatusResponse | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const cached: CachedStatus = JSON.parse(raw);
    if (Date.now() - cached.timestamp > CACHE_TTL_MS) {
      localStorage.removeItem(CACHE_KEY);
      return null;
    }
    return cached.data;
  } catch {
    return null;
  }
}

function setCachedStatus(data: AccessStatusResponse): void {
  const cached: CachedStatus = { data, timestamp: Date.now() };
  localStorage.setItem(CACHE_KEY, JSON.stringify(cached));
}

export function clearAccessStatusCache(): void {
  localStorage.removeItem(CACHE_KEY);
}

interface UseAccessStatusResult {
  status: AccessStatus | null;
  requestId: string | null;
  decisionNote: string | null;
  isLoading: boolean;
  error: Error | null;
  refetch: () => Promise<void>;
}

/**
 * Hook to fetch and cache the user's onboarding access status.
 * Calls /api/access/status once per session (cached for 60s).
 */
export function useAccessStatus(): UseAccessStatusResult {
  const [status, setStatus] = useState<AccessStatus | null>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [decisionNote, setDecisionNote] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const fetchedRef = useRef(false);

  const fetchStatus = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await getAccessStatus();
      setCachedStatus(data);
      setStatus(data.status);
      setRequestId(data.request_id ?? null);
      setDecisionNote(data.decision_note ?? null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Failed to fetch access status'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (fetchedRef.current) return;
    fetchedRef.current = true;

    const cached = getCachedStatus();
    if (cached) {
      setStatus(cached.status);
      setRequestId(cached.request_id ?? null);
      setDecisionNote(cached.decision_note ?? null);
      setIsLoading(false);
      return;
    }

    fetchStatus();
  }, [fetchStatus]);

  return { status, requestId, decisionNote, isLoading, error, refetch: fetchStatus };
}
