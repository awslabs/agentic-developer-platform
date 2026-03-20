import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePagination } from '@/hooks/usePagination';

describe('usePagination', () => {
  it('initializes with default values', () => {
    const { result } = renderHook(() => usePagination());

    expect(result.current.page).toBe(1);
    expect(result.current.pageSize).toBe(50);
    expect(result.current.total).toBe(0);
    expect(result.current.totalPages).toBe(1);
  });

  it('initializes with custom values', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 3, initialPageSize: 25, total: 100 })
    );

    expect(result.current.page).toBe(3);
    expect(result.current.pageSize).toBe(25);
    expect(result.current.total).toBe(100);
    expect(result.current.totalPages).toBe(4);
  });

  it('calculates hasNextPage and hasPrevPage correctly', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 2, initialPageSize: 10, total: 30 })
    );

    expect(result.current.hasPrevPage).toBe(true);
    expect(result.current.hasNextPage).toBe(true);

    // Go to first page
    act(() => {
      result.current.firstPage();
    });
    expect(result.current.hasPrevPage).toBe(false);
    expect(result.current.hasNextPage).toBe(true);

    // Go to last page
    act(() => {
      result.current.lastPage();
    });
    expect(result.current.hasPrevPage).toBe(true);
    expect(result.current.hasNextPage).toBe(false);
  });

  it('navigates to next and previous pages', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 2, initialPageSize: 10, total: 30 })
    );

    act(() => {
      result.current.nextPage();
    });
    expect(result.current.page).toBe(3);

    act(() => {
      result.current.prevPage();
    });
    expect(result.current.page).toBe(2);
  });

  it('does not exceed boundaries on prevPage', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 1, initialPageSize: 10, total: 30 })
    );

    act(() => {
      result.current.prevPage();
    });
    expect(result.current.page).toBe(1); // Should stay at 1
  });

  it('does not exceed boundaries on nextPage', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 1, initialPageSize: 10, total: 30 })
    );

    // First go to last page
    act(() => {
      result.current.lastPage();
    });
    expect(result.current.page).toBe(3);

    // Then try to go past it
    act(() => {
      result.current.nextPage();
    });
    expect(result.current.page).toBe(3); // Should stay at 3
  });

  it('sets page within valid range', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 1, initialPageSize: 10, total: 30 })
    );

    act(() => {
      result.current.setPage(2);
    });
    expect(result.current.page).toBe(2);

    // Try to set page beyond range
    act(() => {
      result.current.setPage(10);
    });
    expect(result.current.page).toBe(3); // Should cap at totalPages

    act(() => {
      result.current.setPage(-1);
    });
    expect(result.current.page).toBe(1); // Should cap at 1
  });

  it('resets page when changing page size', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 3, initialPageSize: 10, total: 100 })
    );

    act(() => {
      result.current.setPageSize(25);
    });
    expect(result.current.page).toBe(1);
    expect(result.current.pageSize).toBe(25);
    expect(result.current.totalPages).toBe(4);
  });

  it('resets to initial values', () => {
    const { result } = renderHook(() =>
      usePagination({ initialPage: 1, initialPageSize: 10, total: 100 })
    );

    act(() => {
      result.current.setPage(5);
      result.current.setPageSize(25);
    });

    act(() => {
      result.current.reset();
    });
    expect(result.current.page).toBe(1);
    expect(result.current.pageSize).toBe(10);
  });

  it('updates total correctly', () => {
    const { result } = renderHook(() => usePagination());

    act(() => {
      result.current.setTotal(100);
    });
    expect(result.current.total).toBe(100);
    expect(result.current.totalPages).toBe(2);
  });
});
