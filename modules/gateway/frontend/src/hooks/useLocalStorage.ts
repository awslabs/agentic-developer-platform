import { useState, useCallback, useEffect } from 'react';

export function useLocalStorage<T>(
  key: string,
  initialValue: T
): [T, (value: T | ((prev: T) => T)) => void, () => void] {
  // Get stored value or use initial
  const readValue = useCallback((): T => {
    if (typeof window === 'undefined') {
      return initialValue;
    }

    try {
      const item = window.localStorage.getItem(key);
      return item ? (JSON.parse(item) as T) : initialValue;
    } catch (error) {
      console.warn(`Error reading localStorage key "${key}":`, error);
      return initialValue;
    }
  }, [key, initialValue]);

  const [storedValue, setStoredValue] = useState<T>(readValue);

  // Set value.
  // Use the functional setState form so `setValue` identity stays stable
  // across renders and the write always sees the latest state. The previous
  // implementation depended on `storedValue`, which meant:
  //   1. Every state change made `setValue` a new reference.
  //   2. Consumers that passed `setValue` into other hooks' useEffect deps
  //      (e.g. useAgUiEvents via onMessagesChange → handleMessagesChange →
  //      setConversations) would see the prop churn every render, causing
  //      WebSocket disconnect/reconnect cycles and lost messages.
  //   3. If a caller captured `setValue` and invoked it later (common with
  //      async WS handlers), the closure held a stale `storedValue` and
  //      could overwrite newer state.
  // The functional form solves both issues. The write to localStorage
  // happens INSIDE the updater — this is deliberate: the updater receives
  // React's latest state synchronously, whereas code after setStoredValue(...)
  // runs before the batched update has resolved (so a "let resolved; ...; if
  // (resolved)" pattern out here silently skips the write under batching).
  // A setItem throw would propagate out of the updater and crash the render,
  // so we wrap it in a local try/catch.
  const setValue = useCallback(
    (value: T | ((prev: T) => T)) => {
      setStoredValue((prev) => {
        const next = value instanceof Function ? value(prev) : value;
        if (typeof window !== 'undefined') {
          try {
            window.localStorage.setItem(key, JSON.stringify(next));
          } catch (error) {
            console.warn(`Error setting localStorage key "${key}":`, error);
          }
        }
        return next;
      });
    },
    [key]
  );

  // Remove value
  const removeValue = useCallback(() => {
    try {
      setStoredValue(initialValue);
      if (typeof window !== 'undefined') {
        window.localStorage.removeItem(key);
      }
    } catch (error) {
      console.warn(`Error removing localStorage key "${key}":`, error);
    }
  }, [key, initialValue]);

  // Listen for changes from other tabs/windows
  useEffect(() => {
    const handleStorageChange = (event: StorageEvent) => {
      if (event.key === key && event.newValue !== null) {
        try {
          setStoredValue(JSON.parse(event.newValue) as T);
        } catch {
          // Ignore parse errors
        }
      }
    };

    window.addEventListener('storage', handleStorageChange);
    return () => window.removeEventListener('storage', handleStorageChange);
  }, [key]);

  return [storedValue, setValue, removeValue];
}

// Session storage variant
export function useSessionStorage<T>(
  key: string,
  initialValue: T
): [T, (value: T | ((prev: T) => T)) => void, () => void] {
  const readValue = useCallback((): T => {
    if (typeof window === 'undefined') {
      return initialValue;
    }

    try {
      const item = window.sessionStorage.getItem(key);
      return item ? (JSON.parse(item) as T) : initialValue;
    } catch (error) {
      console.warn(`Error reading sessionStorage key "${key}":`, error);
      return initialValue;
    }
  }, [key, initialValue]);

  const [storedValue, setStoredValue] = useState<T>(readValue);

  // See useLocalStorage above for rationale on the functional setState form
  // and writing to storage inside the updater.
  const setValue = useCallback(
    (value: T | ((prev: T) => T)) => {
      setStoredValue((prev) => {
        const next = value instanceof Function ? value(prev) : value;
        if (typeof window !== 'undefined') {
          try {
            window.sessionStorage.setItem(key, JSON.stringify(next));
          } catch (error) {
            console.warn(`Error setting sessionStorage key "${key}":`, error);
          }
        }
        return next;
      });
    },
    [key]
  );

  const removeValue = useCallback(() => {
    try {
      setStoredValue(initialValue);
      if (typeof window !== 'undefined') {
        window.sessionStorage.removeItem(key);
      }
    } catch (error) {
      console.warn(`Error removing sessionStorage key "${key}":`, error);
    }
  }, [key, initialValue]);

  return [storedValue, setValue, removeValue];
}
