/**
 * Tests for MainLayout's new-UI entry link — Issue #5079.
 *
 * MainLayout is the current UI's chrome, and the acceptance criterion is that
 * flag-off leaves a working current UI with nothing changed. The risk this file
 * guards is a regression in the *existing* header caused by the one element added
 * to it: the header's own controls must keep rendering either way, and with the
 * flag off the header must not mention the preview at all.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { MainLayout } from '@/layouts/MainLayout';
import { ALL_FEATURES_ENABLED, type FeatureFlags } from '@/services/features';

const mockLogout = vi.fn();
const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

const mockUseFeatures = vi.fn<() => FeatureFlags>();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
  // The entry link reads the revalidating subscription rather than the
  // session-long cache, so that disabling the preview reaches an already-open
  // tab. Both are driven from the same mock here: this file asserts the header's
  // contents per flag state, and the revalidation timing itself is covered in
  // NewUiGate.test.tsx against a real QueryClient.
  useNewUiEnabled: () => ({ enabled: mockUseFeatures().new_ui, isPending: false }),
}));

// Children of the header that do their own fetching — out of scope here.
vi.mock('@/components/WorkspaceSelector', () => ({
  WorkspaceSelector: () => <div data-testid="workspace-selector" />,
}));
vi.mock('@/components/Navigation', () => ({
  Navigation: () => <nav data-testid="navigation" />,
}));
vi.mock('@/components/MobileNav', () => ({
  MobileNav: () => <div data-testid="mobile-nav" />,
}));
vi.mock('@/components/NoOrgBanner', () => ({
  NoOrgBanner: () => <div data-testid="no-org-banner" />,
}));

function renderMainLayout(flags: Partial<FeatureFlags> = {}) {
  mockUseFeatures.mockReturnValue({ ...ALL_FEATURES_ENABLED, ...flags });
  return render(
    <MemoryRouter initialEntries={['/runs']}>
      <Routes>
        <Route element={<MainLayout />}>
          <Route path="/runs" element={<div data-testid="page">Runs</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('MainLayout — new UI entry link (Issue #5079)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({
      user: { id: 'u-1', githubLogin: 'octocat', orgId: 'org-1' },
      logout: mockLogout,
    });
  });

  it('shows "Try the new UI" when the flag is on', () => {
    renderMainLayout({ new_ui: true });
    expect(screen.getByTestId('try-new-ui')).toHaveAttribute('href', '/next');
  });

  it('shows no reference to the preview when the flag is off', () => {
    renderMainLayout({ new_ui: false });
    expect(screen.queryByTestId('try-new-ui')).not.toBeInTheDocument();
    expect(screen.queryByText(/new UI/i)).not.toBeInTheDocument();
  });

  it('keeps rendering the existing header, nav and page with the flag off', () => {
    // The flag-off state is the current UI as it shipped before this story.
    renderMainLayout({ new_ui: false });
    expect(screen.getByTestId('navigation')).toBeInTheDocument();
    expect(screen.getByTestId('mobile-nav')).toBeInTheDocument();
    expect(screen.getByTestId('workspace-selector')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Logout' })).toBeInTheDocument();
    expect(screen.getByTestId('page')).toBeInTheDocument();
  });

  it('keeps rendering the existing header, nav and page with the flag on', () => {
    // Enabling the preview is additive: it must not displace any existing control.
    renderMainLayout({ new_ui: true });
    expect(screen.getByTestId('navigation')).toBeInTheDocument();
    expect(screen.getByTestId('workspace-selector')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Logout' })).toBeInTheDocument();
    expect(screen.getByTestId('page')).toBeInTheDocument();
  });
});
