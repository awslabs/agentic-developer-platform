/**
 * Tests for useFeatures hook — Issue #3566.
 *
 * Verifies fail-open behavior: when the fetch fails or hasn't completed,
 * all features are returned as enabled.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { createElement } from 'react';

// Mock the features service
const mockFetchFeatures = vi.fn();
vi.mock('@/services/features', () => ({
  fetchFeatures: () => mockFetchFeatures(),
  ALL_FEATURES_ENABLED: {
    chat: true,
    knowledge: true,
    indexing: true,
    connections: true,
    credentials: true,
  },
}));

import { useFeatures } from '@/hooks/useFeatures';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  });

  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

describe('useFeatures', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns all-enabled when fetch succeeds with all-true', async () => {
    mockFetchFeatures.mockResolvedValue({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
    });

    const { result } = renderHook(() => useFeatures(), { wrapper: createWrapper() });

    await waitFor(() => {
      expect(result.current.chat).toBe(true);
    });

    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
    });
  });

  it('returns all-enabled when fetch rejects (fail-open)', async () => {
    mockFetchFeatures.mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useFeatures(), { wrapper: createWrapper() });

    // Should immediately return all-enabled (fail-open default)
    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
    });

    // After the query settles (error), still all-enabled
    await waitFor(() => {
      expect(mockFetchFeatures).toHaveBeenCalled();
    });

    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
    });
  });

  it('returns partial flags when some are disabled', async () => {
    mockFetchFeatures.mockResolvedValue({
      chat: false,
      knowledge: true,
      indexing: false,
      connections: true,
      credentials: true,
    });

    const { result } = renderHook(() => useFeatures(), { wrapper: createWrapper() });

    await waitFor(() => {
      expect(result.current.chat).toBe(false);
    });

    expect(result.current.chat).toBe(false);
    expect(result.current.knowledge).toBe(true);
    expect(result.current.indexing).toBe(false);
    expect(result.current.connections).toBe(true);
    expect(result.current.credentials).toBe(true);
  });

  it('returns all-enabled before fetch completes (fail-open while loading)', () => {
    // Never resolves — simulates slow network
    mockFetchFeatures.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useFeatures(), { wrapper: createWrapper() });

    // Immediate value should be all-enabled (the ?? fallback)
    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
    });
  });
});
