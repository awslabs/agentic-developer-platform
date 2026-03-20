import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useLocalStorage, useSessionStorage } from '@/hooks/useLocalStorage';

describe('useLocalStorage', () => {
  let mockLocalStorage: Record<string, string>;
  let originalLocalStorage: Storage;

  beforeEach(() => {
    mockLocalStorage = {};
    originalLocalStorage = window.localStorage;

    // Mock localStorage
    Object.defineProperty(window, 'localStorage', {
      value: {
        getItem: vi.fn((key: string) => mockLocalStorage[key] || null),
        setItem: vi.fn((key: string, value: string) => {
          mockLocalStorage[key] = value;
        }),
        removeItem: vi.fn((key: string) => {
          delete mockLocalStorage[key];
        }),
        clear: vi.fn(() => {
          mockLocalStorage = {};
        }),
      },
      writable: true,
    });
  });

  afterEach(() => {
    Object.defineProperty(window, 'localStorage', {
      value: originalLocalStorage,
      writable: true,
    });
    vi.restoreAllMocks();
  });

  describe('Reading values', () => {
    it('returns initial value when key does not exist', () => {
      const { result } = renderHook(() => useLocalStorage('testKey', 'defaultValue'));

      expect(result.current[0]).toBe('defaultValue');
    });

    it('returns stored value when key exists', () => {
      mockLocalStorage['testKey'] = JSON.stringify('storedValue');

      const { result } = renderHook(() => useLocalStorage('testKey', 'defaultValue'));

      expect(result.current[0]).toBe('storedValue');
    });

    it('returns complex objects from storage', () => {
      const storedObject = { name: 'Test', count: 42, nested: { value: true } };
      mockLocalStorage['complexKey'] = JSON.stringify(storedObject);

      const { result } = renderHook(() =>
        useLocalStorage('complexKey', { name: '', count: 0, nested: { value: false } })
      );

      expect(result.current[0]).toEqual(storedObject);
    });

    it('returns arrays from storage', () => {
      const storedArray = [1, 2, 3, 4, 5];
      mockLocalStorage['arrayKey'] = JSON.stringify(storedArray);

      const { result } = renderHook(() => useLocalStorage<number[]>('arrayKey', []));

      expect(result.current[0]).toEqual(storedArray);
    });

    it('returns initial value when stored value is invalid JSON', () => {
      mockLocalStorage['invalidKey'] = 'not valid json{';
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

      const { result } = renderHook(() => useLocalStorage('invalidKey', 'default'));

      expect(result.current[0]).toBe('default');
      consoleSpy.mockRestore();
    });
  });

  describe('Writing values', () => {
    it('saves value to localStorage', () => {
      const { result } = renderHook(() => useLocalStorage('testKey', 'initial'));

      act(() => {
        result.current[1]('newValue');
      });

      expect(result.current[0]).toBe('newValue');
      expect(window.localStorage.setItem).toHaveBeenCalledWith('testKey', JSON.stringify('newValue'));
    });

    it('saves complex objects to localStorage', () => {
      const { result } = renderHook(() =>
        useLocalStorage<{ name: string; active: boolean }>('objKey', { name: '', active: false })
      );

      const newObj = { name: 'Updated', active: true };
      act(() => {
        result.current[1](newObj);
      });

      expect(result.current[0]).toEqual(newObj);
      expect(window.localStorage.setItem).toHaveBeenCalledWith('objKey', JSON.stringify(newObj));
    });

    it('supports function updater', () => {
      mockLocalStorage['counterKey'] = JSON.stringify(5);

      const { result } = renderHook(() => useLocalStorage('counterKey', 0));

      act(() => {
        result.current[1]((prev) => prev + 1);
      });

      expect(result.current[0]).toBe(6);
    });

    it('handles numbers correctly', () => {
      const { result } = renderHook(() => useLocalStorage('numKey', 0));

      act(() => {
        result.current[1](42);
      });

      expect(result.current[0]).toBe(42);
      expect(window.localStorage.setItem).toHaveBeenCalledWith('numKey', '42');
    });

    it('handles booleans correctly', () => {
      const { result } = renderHook(() => useLocalStorage('boolKey', false));

      act(() => {
        result.current[1](true);
      });

      expect(result.current[0]).toBe(true);
      expect(window.localStorage.setItem).toHaveBeenCalledWith('boolKey', 'true');
    });

    it('handles null values correctly', () => {
      const { result } = renderHook(() => useLocalStorage<string | null>('nullKey', 'initial'));

      act(() => {
        result.current[1](null);
      });

      expect(result.current[0]).toBeNull();
      expect(window.localStorage.setItem).toHaveBeenCalledWith('nullKey', 'null');
    });
  });

  describe('Removing values', () => {
    it('removes value from localStorage and resets to initial', () => {
      mockLocalStorage['removeKey'] = JSON.stringify('stored');

      const { result } = renderHook(() => useLocalStorage('removeKey', 'initial'));

      expect(result.current[0]).toBe('stored');

      act(() => {
        result.current[2]();
      });

      expect(result.current[0]).toBe('initial');
      expect(window.localStorage.removeItem).toHaveBeenCalledWith('removeKey');
    });
  });

  describe('Storage events', () => {
    it('updates value when storage event is fired', () => {
      const { result } = renderHook(() => useLocalStorage('syncKey', 'initial'));

      act(() => {
        const event = new StorageEvent('storage', {
          key: 'syncKey',
          newValue: JSON.stringify('fromOtherTab'),
        });
        window.dispatchEvent(event);
      });

      expect(result.current[0]).toBe('fromOtherTab');
    });

    it('ignores storage events for other keys', () => {
      const { result } = renderHook(() => useLocalStorage('myKey', 'initial'));

      act(() => {
        const event = new StorageEvent('storage', {
          key: 'otherKey',
          newValue: JSON.stringify('changed'),
        });
        window.dispatchEvent(event);
      });

      expect(result.current[0]).toBe('initial');
    });

    it('ignores storage events with null newValue', () => {
      mockLocalStorage['testKey'] = JSON.stringify('stored');
      const { result } = renderHook(() => useLocalStorage('testKey', 'initial'));

      expect(result.current[0]).toBe('stored');

      act(() => {
        const event = new StorageEvent('storage', {
          key: 'testKey',
          newValue: null,
        });
        window.dispatchEvent(event);
      });

      // Should not change the value
      expect(result.current[0]).toBe('stored');
    });
  });

  describe('Error handling', () => {
    it('handles localStorage.getItem errors gracefully', () => {
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      (window.localStorage.getItem as ReturnType<typeof vi.fn>).mockImplementation(() => {
        throw new Error('Storage error');
      });

      const { result } = renderHook(() => useLocalStorage('errorKey', 'default'));

      expect(result.current[0]).toBe('default');
      consoleSpy.mockRestore();
    });

    it('handles localStorage.setItem errors gracefully', () => {
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      (window.localStorage.setItem as ReturnType<typeof vi.fn>).mockImplementation(() => {
        throw new Error('Quota exceeded');
      });

      const { result } = renderHook(() => useLocalStorage('errorKey', 'initial'));

      // Should not throw
      act(() => {
        result.current[1]('newValue');
      });

      consoleSpy.mockRestore();
    });
  });
});

describe('useSessionStorage', () => {
  let mockSessionStorage: Record<string, string>;
  let originalSessionStorage: Storage;

  beforeEach(() => {
    mockSessionStorage = {};
    originalSessionStorage = window.sessionStorage;

    Object.defineProperty(window, 'sessionStorage', {
      value: {
        getItem: vi.fn((key: string) => mockSessionStorage[key] || null),
        setItem: vi.fn((key: string, value: string) => {
          mockSessionStorage[key] = value;
        }),
        removeItem: vi.fn((key: string) => {
          delete mockSessionStorage[key];
        }),
        clear: vi.fn(() => {
          mockSessionStorage = {};
        }),
      },
      writable: true,
    });
  });

  afterEach(() => {
    Object.defineProperty(window, 'sessionStorage', {
      value: originalSessionStorage,
      writable: true,
    });
    vi.restoreAllMocks();
  });

  it('reads from sessionStorage', () => {
    mockSessionStorage['sessionKey'] = JSON.stringify('sessionValue');

    const { result } = renderHook(() => useSessionStorage('sessionKey', 'default'));

    expect(result.current[0]).toBe('sessionValue');
  });

  it('writes to sessionStorage', () => {
    const { result } = renderHook(() => useSessionStorage('sessionKey', 'initial'));

    act(() => {
      result.current[1]('newValue');
    });

    expect(result.current[0]).toBe('newValue');
    expect(window.sessionStorage.setItem).toHaveBeenCalledWith(
      'sessionKey',
      JSON.stringify('newValue')
    );
  });

  it('removes from sessionStorage', () => {
    mockSessionStorage['sessionKey'] = JSON.stringify('stored');

    const { result } = renderHook(() => useSessionStorage('sessionKey', 'default'));

    act(() => {
      result.current[2]();
    });

    expect(result.current[0]).toBe('default');
    expect(window.sessionStorage.removeItem).toHaveBeenCalledWith('sessionKey');
  });

  it('supports function updater', () => {
    mockSessionStorage['counterKey'] = JSON.stringify(10);

    const { result } = renderHook(() => useSessionStorage('counterKey', 0));

    act(() => {
      result.current[1]((prev) => prev * 2);
    });

    expect(result.current[0]).toBe(20);
  });

  it('returns initial value when key does not exist', () => {
    const { result } = renderHook(() => useSessionStorage('nonexistentKey', { value: 42 }));

    expect(result.current[0]).toEqual({ value: 42 });
  });

  it('handles errors gracefully', () => {
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    (window.sessionStorage.getItem as ReturnType<typeof vi.fn>).mockImplementation(() => {
      throw new Error('Storage error');
    });

    const { result } = renderHook(() => useSessionStorage('errorKey', 'default'));

    expect(result.current[0]).toBe('default');
    consoleSpy.mockRestore();
  });
});
