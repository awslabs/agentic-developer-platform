import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useApi } from '@/hooks/useApi';

describe('useApi', () => {
  it('initializes with default state', () => {
    const mockFn = vi.fn().mockResolvedValue('data');
    const { result } = renderHook(() => useApi(mockFn));

    expect(result.current.data).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('executes API call and returns data', async () => {
    const mockData = { id: 1, name: 'Test' };
    const mockFn = vi.fn().mockResolvedValue(mockData);
    const { result } = renderHook(() => useApi(mockFn));

    await act(async () => {
      await result.current.execute();
    });

    expect(result.current.data).toEqual(mockData);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(mockFn).toHaveBeenCalledTimes(1);
  });

  it('handles errors', async () => {
    const errorMessage = 'API Error';
    const mockFn = vi.fn().mockRejectedValue(new Error(errorMessage));
    const { result } = renderHook(() => useApi(mockFn));

    await act(async () => {
      await result.current.execute();
    });

    expect(result.current.data).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeDefined();
    expect(result.current.error?.message).toBe(errorMessage);
  });

  it('passes arguments to API function', async () => {
    const mockFn = vi.fn().mockResolvedValue('result');
    const { result } = renderHook(() => useApi(mockFn));

    await act(async () => {
      await result.current.execute('arg1', 'arg2');
    });

    expect(mockFn).toHaveBeenCalledWith('arg1', 'arg2');
  });

  it('resets state', async () => {
    const mockFn = vi.fn().mockResolvedValue({ data: 'test' });
    const { result } = renderHook(() => useApi(mockFn));

    await act(async () => {
      await result.current.execute();
    });

    expect(result.current.data).not.toBeNull();

    act(() => {
      result.current.reset();
    });

    expect(result.current.data).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('returns data from execute', async () => {
    const mockData = { id: 1 };
    const mockFn = vi.fn().mockResolvedValue(mockData);
    const { result } = renderHook(() => useApi(mockFn));

    let returnedData: { id: number } | null = null;
    await act(async () => {
      returnedData = await result.current.execute() as { id: number } | null;
    });

    expect(returnedData).toEqual(mockData);
  });

  it('returns null from execute on error', async () => {
    const mockFn = vi.fn().mockRejectedValue(new Error('Error'));
    const { result } = renderHook(() => useApi(mockFn));

    let returnedData: unknown = 'not-null';
    await act(async () => {
      returnedData = await result.current.execute();
    });

    expect(returnedData).toBeNull();
  });
});
