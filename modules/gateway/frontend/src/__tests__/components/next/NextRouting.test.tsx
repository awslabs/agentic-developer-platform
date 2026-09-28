/**
 * Wrapper-composition tests for the /next subtree — Issue #5079.
 *
 * SCOPE, stated precisely because an earlier version of this docstring overclaimed
 * it: these tests assemble their own route tree and their own current-UI sentinels.
 * They therefore prove how the wrappers BEHAVE when composed in the documented
 * order — FeatureGate/NewUiGate > ErrorBoundary > Suspense > layout — and nothing
 * about whether `App.tsx` actually composes them that way. They would still pass if
 * the production `/next` route were deleted or moved outside `ProtectedRoute`.
 *
 * Coverage of the shipped configuration lives in `src/__tests__/AppNextRouting.test.tsx`,
 * which renders the real `App` and asserts the production paths and guards. That
 * file is the authority for "the route is wired in and inside the auth guard";
 * this one is the authority for "a rejected lazy import surfaces through Suspense
 * and is caught by the boundary above it", which is far easier to exercise on a
 * tree whose layout element can be swapped for a rejecting import.
 *
 * The two are complementary and both are needed: a mutation that reorders the
 * wrappers fails here, and a mutation that unhooks the route fails there.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Outlet } from 'react-router-dom';
import { Suspense, lazy } from 'react';
import { NewUiGate } from '@/components/next/NewUiGate';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { NextUnavailable } from '@/components/next/NextUnavailable';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

const mockFeatures = vi.fn<() => FeatureFlags>();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockFeatures(),
  useFeaturesQuery: () => ({ data: mockFeatures(), isPending: false }),
  // The preview gate's hook. Stubbed as already-resolved, because the timing of
  // revalidation is covered by NewUiGate.test.tsx against a real QueryClient;
  // here the flag value is just an input to the composition under test.
  useNewUiEnabled: () => ({ enabled: mockFeatures().new_ui, isPending: false }),
}));

/**
 * Build the /next subtree the way App.tsx does, with a swappable layout element
 * so the chunk-failure case can inject a rejecting lazy import.
 *
 * Wrapper order matters and is asserted by the tests below:
 * NewUiGate > ErrorBoundary > Suspense > layout — the same order App.tsx uses.
 */
function renderTree(
  initialPath: string,
  features: Partial<FeatureFlags>,
  // Stands in for NextLayout: an <Outlet /> host, like the real one, so the
  // child-route assertions below exercise real nesting.
  layoutElement: React.ReactNode = (
    <div data-testid="next-layout">
      New UI shell
      <Outlet />
    </div>
  ),
) {
  mockFeatures.mockReturnValue({ ...ALL_FEATURES_ENABLED, ...features });
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        {/* Sentinels for the current-UI routes named in the acceptance criteria. */}
        <Route path="/" element={<div data-testid="current-home">Current home</div>} />
        <Route path="/runs" element={<div data-testid="current-runs">Runs</div>} />
        <Route path="/activity" element={<div data-testid="current-activity">Activity</div>} />
        <Route path="/budgets" element={<div data-testid="current-budgets">Budgets</div>} />
        <Route
          path="/settings/connections"
          element={<div data-testid="current-connections">Connections</div>}
        />
        <Route
          path="/next"
          element={
            <NewUiGate>
              <ErrorBoundary fallback={<NextUnavailable />}>
                <Suspense fallback={<div data-testid="next-loading">Loading</div>}>
                  {layoutElement}
                </Suspense>
              </ErrorBoundary>
            </NewUiGate>
          }
        >
          <Route index element={<div data-testid="next-home">New UI home</div>} />
          <Route path="*" element={<div data-testid="next-not-found">Not in preview</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('/next route subtree — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('flag on', () => {
    it('renders the new shell at /next', async () => {
      renderTree('/next', { new_ui: true });
      expect(await screen.findByTestId('next-layout')).toBeInTheDocument();
      expect(screen.getByTestId('next-home')).toBeInTheDocument();
    });

    it('keeps an unknown /next path inside the shell rather than the app 404', async () => {
      // The scoped catch-all is what keeps "Back to current UI" on screen for a
      // stale or hand-typed preview URL.
      renderTree('/next/does-not-exist', { new_ui: true });
      expect(await screen.findByTestId('next-layout')).toBeInTheDocument();
      expect(screen.getByTestId('next-not-found')).toBeInTheDocument();
    });
  });

  describe('flag off — the documented rollback', () => {
    it('redirects /next to a working current-UI page', async () => {
      renderTree('/next', { new_ui: false });
      expect(await screen.findByTestId('current-home')).toBeInTheDocument();
      expect(screen.queryByTestId('next-layout')).not.toBeInTheDocument();
    });

    it('redirects an unknown /next path too, rather than rendering the preview 404', async () => {
      renderTree('/next/anything', { new_ui: false });
      expect(await screen.findByTestId('current-home')).toBeInTheDocument();
      expect(screen.queryByTestId('next-not-found')).not.toBeInTheDocument();
    });
  });

  describe('new chunk fails to load', () => {
    it('renders the fallback with a hard link into the current UI', async () => {
      // A lazy import that rejects is exactly what a missing chunk looks like to
      // React: the rejection surfaces through Suspense and must be caught by the
      // boundary ABOVE it. If the boundary were nested inside the layout instead,
      // this failure would escape to the app-level boundary and the user would
      // lose the route back.
      const Broken = lazy(() => Promise.reject(new Error('Failed to fetch dynamic module')));
      // React logs the caught error; keep the test output readable.
      const spy = vi.spyOn(console, 'error').mockImplementation(() => {});

      renderTree('/next', { new_ui: true }, <Broken />);

      await waitFor(() => {
        expect(screen.getByTestId('next-unavailable')).toBeInTheDocument();
      });
      const escape = screen.getByTestId('next-unavailable-current-ui');
      expect(escape).toHaveAttribute('href', '/');
      // A full document load, not a client-side navigation: a router Link could
      // re-enter the same broken module graph.
      expect(escape.tagName).toBe('A');
      spy.mockRestore();
    });
  });

  describe('sibling paths are not shadowed by the /next subtree', () => {
    // These use SENTINELS, so they show that a /next route declared alongside
    // other paths does not shadow them — a property of the route shape. The
    // equivalent assertions against the real current-UI pages are in
    // AppNextRouting.test.tsx; that file, not this one, is the regression
    // evidence for the acceptance criterion naming these four URLs.
    it.each([
      ['/runs', 'current-runs'],
      ['/activity', 'current-activity'],
      ['/budgets', 'current-budgets'],
      ['/settings/connections', 'current-connections'],
      ['/', 'current-home'],
    ])('%s still resolves to its own page with the flag on', async (path, testId) => {
      renderTree(path, { new_ui: true });
      expect(await screen.findByTestId(testId)).toBeInTheDocument();
      expect(screen.queryByTestId('next-layout')).not.toBeInTheDocument();
    });

    it('/runs is unaffected by the flag being off', async () => {
      renderTree('/runs', { new_ui: false });
      expect(await screen.findByTestId('current-runs')).toBeInTheDocument();
    });
  });
});
