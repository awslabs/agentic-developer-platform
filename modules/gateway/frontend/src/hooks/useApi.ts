import { useState, useCallback } from 'react';
import type { ApiError } from '@/types/api';

interface UseApiState<T> {
  data: T | null;
  isLoading: boolean;
  error: ApiError | null;
}

interface UseApiReturn<T, Args extends unknown[]> extends UseApiState<T> {
  execute: (...args: Args) => Promise<T | null>;
  reset: () => void;
}

export function useApi<T, Args extends unknown[] = []>(
  apiFunction: (...args: Args) => Promise<T>
): UseApiReturn<T, Args> {
  const [state, setState] = useState<UseApiState<T>>({
    data: null,
    isLoading: false,
    error: null,
  });

  const execute = useCallback(
    async (...args: Args): Promise<T | null> => {
      setState((prev) => ({ ...prev, isLoading: true, error: null }));

      try {
        const data = await apiFunction(...args);
        setState({ data, isLoading: false, error: null });
        return data;
      } catch (err) {
        const error: ApiError = {
          error: 'Request failed',
          message: err instanceof Error ? err.message : 'An unexpected error occurred',
          details: err as Record<string, unknown>,
        };
        setState((prev) => ({ ...prev, isLoading: false, error }));
        return null;
      }
    },
    [apiFunction]
  );

  const reset = useCallback(() => {
    setState({
      data: null,
      isLoading: false,
      error: null,
    });
  }, []);

  return {
    ...state,
    execute,
    reset,
  };
}

// Hook for mutations (POST, PUT, DELETE)
export function useMutation<T, Args extends unknown[] = []>(
  mutationFunction: (...args: Args) => Promise<T>
): UseApiReturn<T, Args> {
  return useApi(mutationFunction);
}
