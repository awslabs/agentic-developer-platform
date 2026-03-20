import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDebounce, useDebouncedCallback } from '@/hooks/useDebounce';

describe('useDebounce', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe('useDebounce hook', () => {
    it('returns initial value immediately', () => {
      const { result } = renderHook(() => useDebounce('initial', 500));

      expect(result.current).toBe('initial');
    });

    it('does not update value before delay', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 'initial', delay: 500 },
      });

      rerender({ value: 'updated', delay: 500 });

      // Advance time but not past the delay
      act(() => {
        vi.advanceTimersByTime(400);
      });

      expect(result.current).toBe('initial');
    });

    it('updates value after delay', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 'initial', delay: 500 },
      });

      rerender({ value: 'updated', delay: 500 });

      act(() => {
        vi.advanceTimersByTime(500);
      });

      expect(result.current).toBe('updated');
    });

    it('resets delay on rapid value changes', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 'initial', delay: 500 },
      });

      // First change
      rerender({ value: 'change1', delay: 500 });
      act(() => {
        vi.advanceTimersByTime(300);
      });

      // Second change before delay completes
      rerender({ value: 'change2', delay: 500 });
      act(() => {
        vi.advanceTimersByTime(300);
      });

      // Value should still be initial because timer reset
      expect(result.current).toBe('initial');

      // Complete the delay from second change
      act(() => {
        vi.advanceTimersByTime(200);
      });

      expect(result.current).toBe('change2');
    });

    it('handles multiple rapid changes correctly', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 'a', delay: 300 },
      });

      // Simulate typing rapidly
      rerender({ value: 'ab', delay: 300 });
      act(() => {
        vi.advanceTimersByTime(100);
      });

      rerender({ value: 'abc', delay: 300 });
      act(() => {
        vi.advanceTimersByTime(100);
      });

      rerender({ value: 'abcd', delay: 300 });
      act(() => {
        vi.advanceTimersByTime(100);
      });

      rerender({ value: 'abcde', delay: 300 });

      // Still original value
      expect(result.current).toBe('a');

      // Complete the delay
      act(() => {
        vi.advanceTimersByTime(300);
      });

      // Should have the final value
      expect(result.current).toBe('abcde');
    });

    it('handles different delay values', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 'initial', delay: 1000 },
      });

      rerender({ value: 'updated', delay: 1000 });

      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(result.current).toBe('initial');

      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(result.current).toBe('updated');
    });

    it('works with number values', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 0, delay: 300 },
      });

      rerender({ value: 42, delay: 300 });

      act(() => {
        vi.advanceTimersByTime(300);
      });

      expect(result.current).toBe(42);
    });

    it('works with object values', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: { name: 'initial' }, delay: 300 },
      });

      const newObj = { name: 'updated' };
      rerender({ value: newObj, delay: 300 });

      act(() => {
        vi.advanceTimersByTime(300);
      });

      expect(result.current).toEqual({ name: 'updated' });
    });

    it('handles zero delay', () => {
      const { result, rerender } = renderHook(({ value, delay }) => useDebounce(value, delay), {
        initialProps: { value: 'initial', delay: 0 },
      });

      rerender({ value: 'updated', delay: 0 });

      act(() => {
        vi.advanceTimersByTime(0);
      });

      expect(result.current).toBe('updated');
    });
  });

  describe('useDebouncedCallback hook', () => {
    it('returns a function', () => {
      const callback = vi.fn();
      const { result } = renderHook(() => useDebouncedCallback(callback, 500));

      expect(typeof result.current).toBe('function');
    });

    it('does not call callback immediately', () => {
      const callback = vi.fn();
      const { result } = renderHook(() => useDebouncedCallback(callback, 500));

      act(() => {
        result.current('arg1');
      });

      expect(callback).not.toHaveBeenCalled();
    });

    it('calls callback after delay', () => {
      const callback = vi.fn();
      const { result } = renderHook(() => useDebouncedCallback(callback, 500));

      act(() => {
        result.current('arg1');
      });

      act(() => {
        vi.advanceTimersByTime(500);
      });

      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith('arg1');
    });

    it('passes all arguments to callback', () => {
      const callback = vi.fn();
      const { result } = renderHook(() => useDebouncedCallback(callback, 500));

      act(() => {
        result.current('arg1', 'arg2', 'arg3');
      });

      act(() => {
        vi.advanceTimersByTime(500);
      });

      expect(callback).toHaveBeenCalledWith('arg1', 'arg2', 'arg3');
    });

    it('only calls callback once for rapid invocations', () => {
      const callback = vi.fn();
      const { result } = renderHook(() => useDebouncedCallback(callback, 500));

      act(() => {
        result.current('call1');
        result.current('call2');
        result.current('call3');
      });

      act(() => {
        vi.advanceTimersByTime(500);
      });

      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith('call3');
    });

    it('resets timer on each invocation', () => {
      const callback = vi.fn();
      const { result } = renderHook(() => useDebouncedCallback(callback, 500));

      act(() => {
        result.current('call1');
      });

      act(() => {
        vi.advanceTimersByTime(400);
      });

      act(() => {
        result.current('call2');
      });

      act(() => {
        vi.advanceTimersByTime(400);
      });

      // Callback should not have been called yet
      expect(callback).not.toHaveBeenCalled();

      act(() => {
        vi.advanceTimersByTime(100);
      });

      // Now it should be called with the latest value
      expect(callback).toHaveBeenCalledTimes(1);
      expect(callback).toHaveBeenCalledWith('call2');
    });

    it('handles callback changes - existing timeout uses original callback', () => {
      const callback1 = vi.fn();
      const callback2 = vi.fn();

      const { result, rerender } = renderHook(({ cb, delay }) => useDebouncedCallback(cb, delay), {
        initialProps: { cb: callback1, delay: 500 },
      });

      act(() => {
        result.current('value1');
      });

      // Change callback before timer fires
      rerender({ cb: callback2, delay: 500 });

      act(() => {
        vi.advanceTimersByTime(500);
      });

      // The existing scheduled timeout still uses the original callback
      // (setTimeout closure captured the old callback reference)
      expect(callback1).toHaveBeenCalledWith('value1');

      // New invocations use the new callback
      act(() => {
        result.current('value2');
      });

      act(() => {
        vi.advanceTimersByTime(500);
      });

      expect(callback2).toHaveBeenCalledWith('value2');
    });

    it('cleans up timeout on unmount', () => {
      const callback = vi.fn();
      const { result, unmount } = renderHook(() => useDebouncedCallback(callback, 500));

      act(() => {
        result.current('value');
      });

      unmount();

      act(() => {
        vi.advanceTimersByTime(500);
      });

      // Callback should not be called after unmount
      expect(callback).not.toHaveBeenCalled();
    });

    it('works with typed arguments', () => {
      interface SearchParams {
        query: string;
        page: number;
      }

      const callback = vi.fn<(params: SearchParams) => void>();
      const { result } = renderHook(() => useDebouncedCallback(callback, 300));

      act(() => {
        result.current({ query: 'test', page: 1 });
      });

      act(() => {
        vi.advanceTimersByTime(300);
      });

      expect(callback).toHaveBeenCalledWith({ query: 'test', page: 1 });
    });
  });
});
