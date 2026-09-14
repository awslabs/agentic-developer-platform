/**
 * Production route-wiring tests for the /next subtree — Issue #5079.
 *
 * These render the REAL `App` component, so the route tree under test is the one
 * that ships. `NextRouting.test.tsx` reassembles an equivalent tree to probe
 * wrapper ORDERING in isolation; the review rightly pointed out that such a test
 * would still pass if the production `/next` route were deleted or moved outside
 * `ProtectedRoute`. These cases close that gap: every assertion below is against
 * `App.tsx`'s own configuration.
 *
 * Mocking strategy — mock at the EDGES, never the thing being asserted:
 *
 * - **Leaf pages** are replaced by sentinels. They are lazy chunks that would each
 *   drag in their own fetches; what matters here is *which* element a path
 *   resolves to, which a sentinel captures exactly.
 * - **`useAuth` and `useAccessStatus`** are stubbed because they are the inputs to
 *   the guards: driving them is how "an unauthenticated session is redirected to
 *   /login" gets exercised.
 * - **`ProtectedRoute`, `OnboardingGuard`, `NewUiGate` and the route tree itself
 *   are NOT mocked.** They are the subject. If `/next` were moved out of the
 *   protected block, the unauthenticated case below would render the preview
 *   instead of the login page and fail.
 *
 * `/features` is served through a real `QueryClient` with a stubbed
 * `fetchFeatures`, so the flag genuinely flows through the cache into the gate.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import App from '@/App';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

// ---------------------------------------------------------------- edges: data

const fetchFeatures = vi.fn<() => Promise<FeatureFlags>>();
const previewPage = vi.hoisted(() => ({
  suspended: false,
  pending: new Promise<never>(() => {}),
}));
vi.mock('@/services/features', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/features')>();
  return { ...actual, fetchFeatures: () => fetchFeatures() };
});

const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => mockUseAuth() }));

const mockUseAccessStatus = vi.fn();
vi.mock('@/hooks/useAccessStatus', () => ({
  useAccessStatus: () => mockUseAccessStatus(),
}));

// A plain member. The full predicate set `Navigation` calls, because the
// current-UI routes below render the real MainLayout: an incomplete stub would
// crash the sidebar and be indistinguishable from a routing regression.
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    isPlatformAdmin: () => false,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    canViewBudgets: () => false,
    canViewRateLimits: () => false,
    canViewLogs: () => false,
    canViewMetrics: () => false,
    canViewOrganizations: () => false,
    canViewPool: () => false,
  }),
}));

// ---------------------------------------------------------------- edges: pages

/** Sentinel page module, in the default-export shape `App.tsx` lazy-imports. */
const page = (testId: string) => ({
  default: () => <div data-testid={testId}>{testId}</div>,
});

vi.mock('@/pages/Login', () => page('page-login'));
vi.mock('@/pages/AgentRunDashboard', () => page('page-runs'));
vi.mock('@/pages/AgentActivity', () => page('page-activity'));
vi.mock('@/pages/BudgetManagement', () => page('page-budgets'));
vi.mock('@/pages/settings/Connections', () => page('page-connections'));
vi.mock('@/pages/NotFound', () => page('page-not-found'));
vi.mock('@/components/RoleBasedRedirect', () => ({
  RoleBasedRedirect: () => <div data-testid="page-home">page-home</div>,
}));

// The preview layout renders WorkspaceSelector, which fetches on mount.
vi.mock('@/components/WorkspaceSelector', () => ({
  WorkspaceSelector: () => <div data-testid="workspace-selector" />,
}));
vi.mock('@/pages/next/NextHome', () => ({
  default: () => {
    if (previewPage.suspended) throw previewPage.pending;
    return <div data-testid="page-next-home">page-next-home</div>;
  },
}));
vi.mock('@/pages/next/NextNotFound', () => page('page-next-404'));

// ---------------------------------------------------------------- harness

function renderApp(path: string, opts: { authenticated?: boolean; newUi?: boolean; pendingFeatures?: boolean } = {}) {
  const { authenticated = true, newUi = true } = opts;

  mockUseAuth.mockReturnValue({
    isAuthenticated: authenticated,
    isLoading: false,
    user: authenticated ? { id: 'u-1', githubLogin: 'octocat' } : null,
    hasPermission: () => true,
    hasRole: () => true,
  });
  mockUseAccessStatus.mockReturnValue({ status: 'registered', isLoading: false });
  fetchFeatures.mockImplementation(() => opts.pendingFeatures
    ? previewPage.pending
    : Promise.resolve({ ...ALL_FEATURES_ENABLED, new_ui: newUi }));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('App.tsx production routing for /next — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    previewPage.suspended = false;
  });

  describe('a stalled preview retains a current-UI escape', () => {
    it('offers a hard link while the initial flag request is pending', async () => {
      renderApp('/next', { pendingFeatures: true });
      expect(await screen.findByTestId('next-gate-loading')).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Back to current UI' })).toHaveAttribute('href', '/');
      expect(screen.queryByTestId('page-home')).not.toBeInTheDocument();
    });

    it('offers an eager hard link while the preview subtree is suspended', async () => {
      previewPage.suspended = true;
      renderApp('/next');
      expect(await screen.findByTestId('next-chunk-loading')).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Back to current UI' })).toHaveAttribute('href', '/');
      expect(screen.queryByTestId('page-next-home')).not.toBeInTheDocument();
    });
  });

  describe('the preview is wired into the real route tree', () => {
    it('serves the preview shell at /next when the flag is on', async () => {
      renderApp('/next');
      expect(await screen.findByTestId('page-next-home')).toBeInTheDocument();
      // The production layout, not a stand-in.
      expect(screen.getByTestId('next-layout')).toBeInTheDocument();
    });

    it('keeps an unknown /next path inside the preview, not the app-level 404', async () => {
      renderApp('/next/nope');
      expect(await screen.findByTestId('page-next-404')).toBeInTheDocument();
      expect(screen.getByTestId('next-layout')).toBeInTheDocument();
      expect(screen.queryByTestId('page-not-found')).not.toBeInTheDocument();
    });

    it('renders the return link, so the way back is part of the shipped shell', async () => {
      renderApp('/next');
      await screen.findByTestId('page-next-home');
      expect(screen.getByTestId('back-to-current-ui')).toHaveAttribute('href', '/');
    });
  });

  describe('/next is inside the existing authentication guard', () => {
    // This is the criterion "both interfaces share identity; no second login".
    // It is a property of WHERE the route sits, so only a production-tree test can
    // establish it: if /next were hoisted out of ProtectedRoute, the preview would
    // render here for a signed-out visitor.
    it('sends an unauthenticated visitor to /login instead of the preview', async () => {
      renderApp('/next', { authenticated: false });
      expect(await screen.findByTestId('page-login')).toBeInTheDocument();
      expect(screen.queryByTestId('next-layout')).not.toBeInTheDocument();
    });

    it('sends an unauthenticated visitor to /login from a deep preview path too', async () => {
      renderApp('/next/anything', { authenticated: false });
      expect(await screen.findByTestId('page-login')).toBeInTheDocument();
    });

    it('applies the same guard to a current-UI route, showing the guard is shared', async () => {
      // The comparison that makes the two cases above meaningful: /runs and /next
      // fail closed for a signed-out visitor in exactly the same way, because they
      // are inside the same ProtectedRoute block.
      renderApp('/runs', { authenticated: false });
      expect(await screen.findByTestId('page-login')).toBeInTheDocument();
    });
  });

  describe('flag off — the documented rollback, in the shipped tree', () => {
    it('redirects /next to a working current-UI page', async () => {
      renderApp('/next', { newUi: false });
      expect(await screen.findByTestId('page-home')).toBeInTheDocument();
      expect(screen.queryByTestId('next-layout')).not.toBeInTheDocument();
    });

    it('redirects a deep preview path too', async () => {
      renderApp('/next/nope', { newUi: false });
      expect(await screen.findByTestId('page-home')).toBeInTheDocument();
      expect(screen.queryByTestId('page-next-404')).not.toBeInTheDocument();
    });
  });

  describe('current-UI routes named in the acceptance criteria are unchanged', () => {
    // Adding the /next block must not shadow, reorder or re-guard any of these.
    // Asserted against the production tree in BOTH flag states, because the
    // criterion is that existing URLs are unaffected either way.
    it.each([
      ['/runs', 'page-runs'],
      ['/activity', 'page-activity'],
      ['/budgets', 'page-budgets'],
      ['/settings/connections', 'page-connections'],
      ['/', 'page-home'],
    ])('%s still resolves to its current-UI page with the flag ON', async (path, testId) => {
      renderApp(path, { newUi: true });
      expect(await screen.findByTestId(testId)).toBeInTheDocument();
      expect(screen.queryByTestId('next-layout')).not.toBeInTheDocument();
    });

    it.each([
      ['/runs', 'page-runs'],
      ['/activity', 'page-activity'],
      ['/budgets', 'page-budgets'],
      ['/settings/connections', 'page-connections'],
    ])('%s still resolves to its current-UI page with the flag OFF', async (path, testId) => {
      renderApp(path, { newUi: false });
      expect(await screen.findByTestId(testId)).toBeInTheDocument();
    });

    it('an unknown non-preview path still reaches the app-level 404', async () => {
      renderApp('/no-such-page');
      expect(await screen.findByTestId('page-not-found')).toBeInTheDocument();
    });
  });
});
