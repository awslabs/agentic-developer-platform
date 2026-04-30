/**
 * Resilience Tests — Issue #288
 *
 * Validates three defensive patterns:
 * 1. Service methods return [] (not crash) when API response is malformed
 * 2. Toast dedup prevents stacking identical error messages
 * 3. Verifies null-safe handling for various bad response shapes
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { apiClient } from '@/services/api';
import { getBudgets, getBudgetsWithUtilization, getUsage, getUsageTimeSeries } from '@/services/budget';
import { getRatelimits } from '@/services/ratelimit';
import { getLogs } from '@/services/logs';
import { getOrganizations, getDepartments, getTeams, getUserRoles } from '@/services/admin';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  buildQueryString: vi.fn(() => ''),
}));

describe('Service Layer Resilience — Null-safe API responses', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const malformedResponses = [
    { label: 'empty object', value: {} },
    { label: 'items is null', value: { items: null, total: 0, page: 1, page_size: 20, has_more: false } },
    { label: 'items is undefined', value: { items: undefined, total: 0, page: 1, page_size: 20, has_more: false } },
    { label: 'items is a string (HTML)', value: { items: '<!DOCTYPE html><html>' } },
    { label: 'entirely null response', value: null },
  ];

  describe('getBudgets', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getBudgets('org-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getBudgetsWithUtilization', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getBudgetsWithUtilization('org-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getUsage', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getUsage('org-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getUsageTimeSeries', () => {
    const timeSeriesMalformed = [
      { label: 'empty object', value: {} },
      { label: 'data is null', value: { data: null, period: 'daily', org_id: 'x' } },
      { label: 'data is undefined', value: { data: undefined, period: 'daily', org_id: 'x' } },
      { label: 'entirely null response', value: null },
    ];

    it.each(timeSeriesMalformed)('returns empty array when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getUsageTimeSeries('org-1', {
        startDate: '2024-01-01',
        endDate: '2024-01-07',
      });
      expect(result).toEqual([]);
    });
  });

  describe('getRatelimits', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getRatelimits('org-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getLogs', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getLogs();
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getOrganizations', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getOrganizations();
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getDepartments', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getDepartments('org-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getTeams', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getTeams('org-1', 'dept-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });

  describe('getUserRoles', () => {
    it.each(malformedResponses)('returns empty items when response is $label', async ({ value }) => {
      vi.mocked(apiClient.get).mockResolvedValue(value);
      const result = await getUserRoles('org-1');
      expect(result.items).toEqual([]);
      expect(result.total).toBe(0);
    });
  });
});

describe('Toast Dedup — same error does not stack', () => {
  it('prevents duplicate toasts with same type and message', async () => {
    // Dynamically import the actual ToastContext to test dedup
    const { ToastProvider, useToast } = await import('@/contexts/ToastContext');
    const React = await import('react');

    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(ToastProvider, null, children);

    const { result } = renderHook(() => useToast(), { wrapper });

    // Fire the same error twice
    act(() => {
      result.current.error('Failed to load budgets');
    });
    act(() => {
      result.current.error('Failed to load budgets');
    });

    // Only 1 toast should be visible (dedup)
    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0].message).toBe('Failed to load budgets');
  });

  it('allows different messages to stack', async () => {
    const { ToastProvider, useToast } = await import('@/contexts/ToastContext');
    const React = await import('react');

    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(ToastProvider, null, children);

    const { result } = renderHook(() => useToast(), { wrapper });

    act(() => {
      result.current.error('Error A');
    });
    act(() => {
      result.current.error('Error B');
    });

    expect(result.current.toasts).toHaveLength(2);
  });

  it('allows same message after previous is dismissed', async () => {
    const { ToastProvider, useToast } = await import('@/contexts/ToastContext');
    const React = await import('react');

    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(ToastProvider, null, children);

    const { result } = renderHook(() => useToast(), { wrapper });

    act(() => {
      result.current.error('Error A');
    });

    const toastId = result.current.toasts[0].id;
    act(() => {
      result.current.removeToast(toastId);
    });

    // Now same message should be allowed again
    act(() => {
      result.current.error('Error A');
    });

    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0].message).toBe('Error A');
  });
});
