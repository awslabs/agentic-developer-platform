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
    gitlab: false,
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

  it('returns all-enabled when fetch succeeds with all-true (gitlab from server)', async () => {
    mockFetchFeatures.mockResolvedValue({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
      gitlab: true,
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
      gitlab: true,
    });
  });

  it('returns fail-open defaults when fetch rejects (gitlab stays false)', async () => {
    mockFetchFeatures.mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useFeatures(), { wrapper: createWrapper() });

    // Should immediately return fail-open defaults (gitlab: false is the exception)
    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
      gitlab: false,
    });

    // After the query settles (error), same defaults
    await waitFor(() => {
      expect(mockFetchFeatures).toHaveBeenCalled();
    });

    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
      gitlab: false,
    });
  });

  it('returns partial flags when some are disabled', async () => {
    mockFetchFeatures.mockResolvedValue({
      chat: false,
      knowledge: true,
      indexing: false,
      connections: true,
      credentials: true,
      gitlab: false,
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
    expect(result.current.gitlab).toBe(false);
  });

  it('returns fail-open defaults before fetch completes (gitlab stays false)', () => {
    // Never resolves — simulates slow network
    mockFetchFeatures.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useFeatures(), { wrapper: createWrapper() });

    // Immediate value should be fail-open defaults (gitlab: false is the exception)
    expect(result.current).toEqual({
      chat: true,
      knowledge: true,
      indexing: true,
      connections: true,
      credentials: true,
      gitlab: false,
    });
  });
});
