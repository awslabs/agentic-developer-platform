import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useFlowPollInterval } from '@/hooks/useFlowPollInterval';

afterEach(() => vi.restoreAllMocks());

describe('flow polling interval', () => {
  it('spreads clients within the freshness limit and stays stable across renders', () => {
    const random = vi.spyOn(Math, 'random').mockReturnValueOnce(0).mockReturnValueOnce(0.99);
    const first = renderHook(() => useFlowPollInterval(30_000));
    const second = renderHook(() => useFlowPollInterval(30_000));
    expect(first.result.current).toBe(27_000);
    expect(second.result.current).toBeGreaterThan(first.result.current);
    expect(second.result.current).toBeLessThanOrEqual(30_000);
    first.rerender();
    expect(first.result.current).toBe(27_000);
    expect(random).toHaveBeenCalledTimes(2);
  });
});
