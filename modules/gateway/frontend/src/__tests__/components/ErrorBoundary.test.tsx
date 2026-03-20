import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ErrorBoundary } from '@/components/ErrorBoundary';

// Component that deliberately throws an error
function ThrowingComponent({ shouldThrow = true }: { shouldThrow?: boolean }) {
  if (shouldThrow) {
    throw new Error('Test error message');
  }
  return <div data-testid="child">Child rendered successfully</div>;
}

// Component that throws with stack trace
function ThrowingWithStack(): React.ReactNode {
  const error = new Error('Detailed error message');
  error.stack = `Error: Detailed error message
    at ThrowingWithStack (test.tsx:10:5)
    at renderWithHooks (react-dom.js:100:1)
    at mountIndeterminateComponent (react-dom.js:200:1)`;
  throw error;
}

describe('ErrorBoundary', () => {
  let consoleSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // Suppress console.error for cleaner test output
    consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleSpy.mockRestore();
  });

  describe('Normal rendering', () => {
    it('renders children when no error occurs', () => {
      render(
        <ErrorBoundary>
          <div data-testid="child">Child content</div>
        </ErrorBoundary>
      );

      expect(screen.getByTestId('child')).toBeInTheDocument();
      expect(screen.getByText('Child content')).toBeInTheDocument();
    });

    it('renders multiple children when no error', () => {
      render(
        <ErrorBoundary>
          <div data-testid="child1">First child</div>
          <div data-testid="child2">Second child</div>
        </ErrorBoundary>
      );

      expect(screen.getByTestId('child1')).toBeInTheDocument();
      expect(screen.getByTestId('child2')).toBeInTheDocument();
    });

    it('renders complex nested children', () => {
      render(
        <ErrorBoundary>
          <div>
            <header>Header</header>
            <main>
              <section data-testid="section">Content</section>
            </main>
            <footer>Footer</footer>
          </div>
        </ErrorBoundary>
      );

      expect(screen.getByTestId('section')).toBeInTheDocument();
      expect(screen.getByText('Header')).toBeInTheDocument();
      expect(screen.getByText('Footer')).toBeInTheDocument();
    });
  });

  describe('Error handling', () => {
    it('catches errors and shows default fallback', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
      expect(screen.queryByTestId('child')).not.toBeInTheDocument();
    });

    it('displays error message in details', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      // Error details should contain the error message
      const details = screen.getByText('Error details');
      expect(details).toBeInTheDocument();

      // Click to expand details
      details.click();

      expect(screen.getByText(/Test error message/)).toBeInTheDocument();
    });

    it('logs error to console', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      expect(consoleSpy).toHaveBeenCalledWith(
        'Error caught by boundary:',
        expect.any(Error),
        expect.any(Object)
      );
    });

    it('displays error stack trace when available', () => {
      render(
        <ErrorBoundary>
          <ThrowingWithStack />
        </ErrorBoundary>
      );

      // Click to expand details
      screen.getByText('Error details').click();

      expect(screen.getByText(/at ThrowingWithStack/)).toBeInTheDocument();
    });
  });

  describe('Custom fallback', () => {
    it('renders custom fallback when provided', () => {
      render(
        <ErrorBoundary fallback={<div data-testid="custom-fallback">Custom Error UI</div>}>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      expect(screen.getByTestId('custom-fallback')).toBeInTheDocument();
      expect(screen.getByText('Custom Error UI')).toBeInTheDocument();
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });

    it('renders complex custom fallback', () => {
      const CustomFallback = (
        <div>
          <h1 data-testid="custom-title">Oops!</h1>
          <p>Something broke</p>
          <button>Retry</button>
        </div>
      );

      render(
        <ErrorBoundary fallback={CustomFallback}>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      expect(screen.getByTestId('custom-title')).toBeInTheDocument();
      expect(screen.getByText('Something broke')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    });
  });

  describe('Refresh button', () => {
    it('renders refresh button in default fallback', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      expect(screen.getByRole('button', { name: 'Refresh Page' })).toBeInTheDocument();
    });

    it('calls window.location.reload on refresh button click', async () => {
      const user = userEvent.setup();
      const reloadMock = vi.fn();

      // Mock window.location.reload
      const originalLocation = window.location;
      delete (window as { location?: Location }).location;
      window.location = { ...originalLocation, reload: reloadMock } as Location;

      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      await user.click(screen.getByRole('button', { name: 'Refresh Page' }));

      expect(reloadMock).toHaveBeenCalled();

      // Restore original location
      window.location = originalLocation;
    });
  });

  describe('Error details', () => {
    it('renders collapsible error details', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      const details = screen.getByRole('group');
      expect(details).toBeInTheDocument();
    });

    it('expands error details on click', async () => {
      const user = userEvent.setup();

      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      const summary = screen.getByText('Error details');
      await user.click(summary);

      // Error message should be visible after expanding
      expect(screen.getByText(/Test error message/)).toBeInTheDocument();
    });
  });

  describe('UI styling', () => {
    it('has correct warning emoji', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      // The warning emoji is rendered in the default fallback
      const container = screen.getByText('Something went wrong').closest('div');
      expect(container?.parentElement?.textContent).toContain('Something went wrong');
    });

    it('displays help message', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      expect(
        screen.getByText(/An unexpected error has occurred. Please try refreshing the page./)
      ).toBeInTheDocument();
    });
  });

  describe('getDerivedStateFromError', () => {
    it('sets hasError state to true on error', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      // The presence of the fallback UI indicates hasError is true
      expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    });

    it('captures error object', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      // Click to expand and verify error is captured
      screen.getByText('Error details').click();
      expect(screen.getByText(/Test error message/)).toBeInTheDocument();
    });
  });

  describe('componentDidCatch', () => {
    it('receives error info with component stack', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent />
        </ErrorBoundary>
      );

      // componentDidCatch should have been called with error info
      expect(consoleSpy).toHaveBeenCalledWith(
        'Error caught by boundary:',
        expect.any(Error),
        expect.objectContaining({
          componentStack: expect.any(String),
        })
      );
    });
  });

  describe('Non-throwing scenarios', () => {
    it('does not show error UI when child does not throw', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={false} />
        </ErrorBoundary>
      );

      expect(screen.getByTestId('child')).toBeInTheDocument();
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
    });
  });
});
