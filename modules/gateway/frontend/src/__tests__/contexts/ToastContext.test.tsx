import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { renderHook } from '@testing-library/react';
import { ToastProvider, useToast } from '@/contexts/ToastContext';
import type { ReactNode } from 'react';

function createWrapper() {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <ToastProvider>{children}</ToastProvider>;
  };
}

// Test component that uses toast
function TestComponent() {
  const { success, error, warning, info, toasts, removeToast } = useToast();

  return (
    <div>
      <button onClick={() => success('Success message')} data-testid="success-btn">
        Show Success
      </button>
      <button onClick={() => error('Error message')} data-testid="error-btn">
        Show Error
      </button>
      <button onClick={() => warning('Warning message')} data-testid="warning-btn">
        Show Warning
      </button>
      <button onClick={() => info('Info message')} data-testid="info-btn">
        Show Info
      </button>
      <button onClick={() => success('Persistent', 0)} data-testid="persistent-btn">
        Show Persistent
      </button>
      <div data-testid="toast-count">{toasts.length}</div>
      {toasts.map((toast) => (
        <button
          key={toast.id}
          onClick={() => removeToast(toast.id)}
          data-testid={`remove-${toast.id}`}
        >
          Remove {toast.id}
        </button>
      ))}
    </div>
  );
}

describe('ToastContext', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe('ToastProvider', () => {
    it('renders children correctly', () => {
      render(
        <ToastProvider>
          <div data-testid="child">Child Content</div>
        </ToastProvider>
      );

      expect(screen.getByTestId('child')).toBeInTheDocument();
      expect(screen.getByText('Child Content')).toBeInTheDocument();
    });

    it('initializes with empty toasts array', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      expect(result.current.toasts).toEqual([]);
    });
  });

  describe('Toast creation', () => {
    it('creates success toast', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('success-btn'));

      expect(screen.getByText('Success message')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveClass('bg-green-500');
    });

    it('creates error toast', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('error-btn'));

      expect(screen.getByText('Error message')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveClass('bg-red-500');
    });

    it('creates warning toast', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('warning-btn'));

      expect(screen.getByText('Warning message')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveClass('bg-yellow-500');
    });

    it('creates info toast', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('info-btn'));

      expect(screen.getByText('Info message')).toBeInTheDocument();
      expect(screen.getByRole('alert')).toHaveClass('bg-blue-500');
    });
  });

  describe('Toast auto-dismiss', () => {
    it('auto-dismisses toast after default timeout (5000ms)', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('success-btn'));
      expect(screen.getByText('Success message')).toBeInTheDocument();

      // Fast-forward past the default timeout
      act(() => {
        vi.advanceTimersByTime(5000);
      });

      expect(screen.queryByText('Success message')).not.toBeInTheDocument();
    });

    it('does not auto-dismiss when duration is 0', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('persistent-btn'));
      expect(screen.getByText('Persistent')).toBeInTheDocument();

      // Fast-forward well past the default timeout
      act(() => {
        vi.advanceTimersByTime(10000);
      });

      // Should still be visible
      expect(screen.getByText('Persistent')).toBeInTheDocument();
    });
  });

  describe('Manual toast removal', () => {
    it('removes toast manually', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('success-btn'));
      expect(screen.getByText('Success message')).toBeInTheDocument();

      // Find and click the dismiss button on the toast
      const dismissButton = screen.getByLabelText('Dismiss notification');
      fireEvent.click(dismissButton);

      expect(screen.queryByText('Success message')).not.toBeInTheDocument();
    });

    it('removes specific toast by id', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      // Add two toasts
      act(() => {
        result.current.success('Toast 1');
        result.current.error('Toast 2');
      });

      expect(result.current.toasts).toHaveLength(2);

      // Remove the first toast
      const firstToastId = result.current.toasts[0].id;
      act(() => {
        result.current.removeToast(firstToastId);
      });

      expect(result.current.toasts).toHaveLength(1);
      expect(result.current.toasts[0].message).toBe('Toast 2');
    });
  });

  describe('Multiple toasts', () => {
    it('handles multiple toasts simultaneously', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('success-btn'));
      fireEvent.click(screen.getByTestId('error-btn'));
      fireEvent.click(screen.getByTestId('info-btn'));

      expect(screen.getByTestId('toast-count')).toHaveTextContent('3');
      expect(screen.getAllByRole('alert')).toHaveLength(3);
    });

    it('dismisses toasts independently', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('success-btn'));

      // Wait a bit before adding more
      act(() => {
        vi.advanceTimersByTime(1000);
      });

      fireEvent.click(screen.getByTestId('error-btn'));

      expect(screen.getAllByRole('alert')).toHaveLength(2);

      // Fast-forward to dismiss first toast (4 more seconds)
      act(() => {
        vi.advanceTimersByTime(4000);
      });

      expect(screen.getAllByRole('alert')).toHaveLength(1);

      // The error toast should still be visible (1 second remaining)
      expect(screen.getByText('Error message')).toBeInTheDocument();
    });
  });

  describe('addToast function', () => {
    it('adds toast with custom duration', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.addToast('info', 'Custom duration', 10000);
      });

      expect(result.current.toasts).toHaveLength(1);
      expect(result.current.toasts[0].duration).toBe(10000);
    });

    it('generates unique IDs for each toast', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.addToast('success', 'Toast 1');
        result.current.addToast('success', 'Toast 2');
        result.current.addToast('success', 'Toast 3');
      });

      const ids = result.current.toasts.map((t) => t.id);
      const uniqueIds = new Set(ids);

      expect(uniqueIds.size).toBe(3);
    });
  });

  describe('Toast helper functions', () => {
    it('success helper creates success toast', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.success('Success!');
      });

      expect(result.current.toasts[0].type).toBe('success');
      expect(result.current.toasts[0].message).toBe('Success!');
    });

    it('error helper creates error toast', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.error('Error!');
      });

      expect(result.current.toasts[0].type).toBe('error');
      expect(result.current.toasts[0].message).toBe('Error!');
    });

    it('warning helper creates warning toast', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.warning('Warning!');
      });

      expect(result.current.toasts[0].type).toBe('warning');
      expect(result.current.toasts[0].message).toBe('Warning!');
    });

    it('info helper creates info toast', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.info('Info!');
      });

      expect(result.current.toasts[0].type).toBe('info');
      expect(result.current.toasts[0].message).toBe('Info!');
    });

    it('helpers accept custom duration', () => {
      const { result } = renderHook(() => useToast(), {
        wrapper: createWrapper(),
      });

      act(() => {
        result.current.success('Custom duration', 3000);
      });

      expect(result.current.toasts[0].duration).toBe(3000);
    });
  });

  describe('ToastContainer rendering', () => {
    it('does not render container when no toasts', () => {
      render(
        <ToastProvider>
          <div>Content</div>
        </ToastProvider>
      );

      expect(screen.queryByRole('region')).not.toBeInTheDocument();
    });

    it('renders container with correct aria label', () => {
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      fireEvent.click(screen.getByTestId('success-btn'));

      expect(screen.getByRole('region', { name: 'Notifications' })).toBeInTheDocument();
    });
  });

  describe('useToast outside provider', () => {
    it('throws error when used outside ToastProvider', () => {
      // Suppress console.error for this test
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

      expect(() => {
        renderHook(() => useToast());
      }).toThrow('useToast must be used within a ToastProvider');

      consoleSpy.mockRestore();
    });
  });
});
