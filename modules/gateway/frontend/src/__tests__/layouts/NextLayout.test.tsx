/**
 * Tests for NextLayout — Issue #5079.
 *
 * The layout carries two acceptance criteria: the return link is persistent (on
 * every preview page, not just the home page), and both interfaces share identity
 * and the active org/workspace rather than keeping parallel copies.
 *
 * These remain #5079's assertions and are deliberately unchanged by #5080 — they
 * are the regression signal that adding the two journeys did not disturb the
 * shell's return link, shared identity or preview labelling. #5080 added the
 * feature and permission hooks the layout now reads, so the harness stubs those
 * below; the journey behaviour itself is asserted in NextLayoutJourneys.test.tsx.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { NextLayout } from '@/layouts/NextLayout';
import { ALL_FEATURES_ENABLED } from '@/services/features';

const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

// #5080: the layout builds its navigation from these. Stubbed rather than wrapped
// in providers because this file's assertions are about the shell chrome, not the
// journey model — a real QueryClient here would only add a fetch to mock.
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => ALL_FEATURES_ENABLED,
}));

// A plain member: no roles and no permissions, so the Administration journey is
// absent and these tests exercise the simplest shell.
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    isPlatformAdmin: () => false,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    canViewOrganizations: () => false,
    canViewBudgets: () => false,
    canViewRateLimits: () => false,
    canViewLogs: () => false,
    canViewPool: () => false,
    canViewMetrics: () => false,
  }),
}));

// The real WorkspaceSelector fetches on mount; the assertion that matters here is
// that the layout renders THE SAME component the current UI renders, which this
// sentinel captures without a network stub.
vi.mock('@/components/WorkspaceSelector', () => ({
  WorkspaceSelector: () => <div data-testid="workspace-selector">Organization</div>,
}));

function renderLayout(initialPath = '/next') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/" element={<div data-testid="current-home">Current home</div>} />
        <Route path="/next" element={<NextLayout />}>
          <Route index element={<div data-testid="preview-home">Preview home</div>} />
          <Route path="deep/page" element={<div data-testid="preview-deep">Deep preview page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('NextLayout — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({ user: { id: 'u-1', githubLogin: 'octocat' } });
  });

  it('renders the return link on the preview home', () => {
    renderLayout();
    const back = screen.getByTestId('back-to-current-ui');
    expect(back).toHaveAttribute('href', '/');
    expect(back).toHaveTextContent('Back to current UI');
  });

  it('renders the return link on a nested preview page too', () => {
    // "Persistent" is the requirement — a return link only on the landing page
    // would strand a user who navigated deeper.
    renderLayout('/next/deep/page');
    expect(screen.getByTestId('preview-deep')).toBeInTheDocument();
    expect(screen.getByTestId('back-to-current-ui')).toBeInTheDocument();
  });

  it('renders the shared WorkspaceSelector so org context is not duplicated', () => {
    renderLayout();
    expect(screen.getByTestId('workspace-selector')).toBeInTheDocument();
  });

  it('shows the signed-in identity from the shared auth state', () => {
    // Same useAuth the current UI reads: one session, no second login.
    renderLayout();
    expect(screen.getByText('octocat')).toBeInTheDocument();
  });

  it('omits the identity row and workspace selector when there is no user', () => {
    // The guards above this layout normally prevent it, but the layout must not
    // throw if auth state is momentarily empty.
    mockUseAuth.mockReturnValue({ user: null });
    renderLayout();
    expect(screen.queryByTestId('workspace-selector')).not.toBeInTheDocument();
    expect(screen.getByTestId('back-to-current-ui')).toBeInTheDocument();
  });

  it('labels itself as a preview so nobody mistakes it for the default UI', () => {
    renderLayout();
    expect(screen.getByText('New UI preview')).toBeInTheDocument();
  });
});
